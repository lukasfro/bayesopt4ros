from __future__ import annotations

import torch
import yaml

from torch import Tensor
from typing import Tuple, List

from botorch.models import SingleTaskGP
from botorch.acquisition import AcquisitionFunction, FixedFeatureAcquisitionFunction
from botorch.models.gpytorch import GPyTorchModel
from botorch.optim import optimize_acqf as optimize_acqf_botorch
from botorch.models.transforms.input import Normalize
from botorch.models.transforms.outcome import Standardize

from gpytorch.kernels import MaternKernel, ScaleKernel
from gpytorch.priors import GammaPrior

from bayesopt4ros import BayesianOptimization
from bayesopt4ros.util import PosteriorMean


class ContextualBayesianOptimization(BayesianOptimization):
    """The contextual Bayesian optimization class.

    Implements the actual heavy lifting that is done under the hood of
    :class:`contextual_bayesopt_server.ContextualBayesOptServer`.

    """

    def __init__(
        self,
        input_dim: int,
        context_dim: int,
        max_iter: int,
        bounds: Tensor,
        context_bounds: Tensor,
        acq_func: str = "UCB",
        n_init: int = 5,
        log_dir: str = None,
        load_dir: str = None,
        config: dict = None,
        maximize: bool = True,
    ) -> None:
        """The ContextualBayesianOptimization class initializer.

        .. note:: For a definition of the other arguments, see
            :class:`bayesopt.BayesianOptimization`.

        Parameters
        ----------
        context_dim : int
            Number of context dimensions for the parameters.
        context_bounds : torch.Tensor
            A [2, context_dim] shaped tensor specifying the context variables domain.
        """
        super().__init__(
            input_dim=input_dim,
            max_iter=max_iter,
            bounds=bounds,
            acq_func=acq_func,
            n_init=n_init,
            log_dir=log_dir,
            load_dir=load_dir,
            config=config,
            maximize=maximize,
        )
        self.context, self.prev_context = None, None
        self.context_dim = context_dim
        self.context_bounds = context_bounds
        self.joint_dim = self.input_dim + self.context_dim
        self.joint_bounds = torch.cat((self.bounds, self.context_bounds), dim=1)

    @classmethod
    def from_file(cls, config_file: str) -> ContextualBayesianOptimization:
        # TODO(lukasfro): Does not feel right to copy that much code from base class
        """Initialize a ContextualBayesianOptimization instance from a config file.

        Parameters
        ----------
        config_file : str
            The config file (full path, relative or absolute).

        Returns
        -------
        :class:`ContextualBayesianOptimization`
            An instance of the ContextualBayesianOptimization class.
        """
        # Read config from file
        with open(config_file, "r") as f:
            config = yaml.load(f, Loader=yaml.FullLoader)

        # Bring bounds in correct format
        lb = torch.tensor(config["lower_bound"])
        ub = torch.tensor(config["upper_bound"])
        bounds = torch.stack((lb, ub))

        lbc = torch.tensor(config["lower_bound_context"])
        ubc = torch.tensor(config["upper_bound_context"])
        context_bounds = torch.stack((lbc, ubc))

        # Construct class instance based on the config
        return cls(
            input_dim=config["input_dim"],
            context_dim=config["context_dim"],
            max_iter=config["max_iter"],
            bounds=bounds,
            context_bounds=context_bounds,
            acq_func=config["acq_func"],
            n_init=config["n_init"],
            log_dir=config.get("log_dir"),
            load_dir=config.get("load_dir"),
            maximize=config["maximize"],
            config=config,
        )

    def get_best_observation(self) -> Tuple[torch.Tensor, torch.Tensor, float]:
        """Get the best parameters, context and corresponding observed value."""
        x_best, c_best = torch.split(self.x_best, [self.input_dim, self.context_dim])
        return x_best, c_best, self.y_best

    def get_optimal_parameters(self, context=None) -> Tuple[torch.Tensor, float]:
        """Geth the optimal parameters for given context with corresponding value."""
        return self._optimize_posterior_mean(context)

    @property
    def constant_config_parameters(self) -> List[str]:
        """These parameters need to be the same when loading previous runs. For
        all other settings, the user might have a reasonable explanation to
        change it inbetween experiments/runs. E.g., maximum number of iterations
        or bounds.

        See Also
        --------
        _check_config
        """
        return ["input_dim", "context_dim", "maximize"]

    def _update_model(self, goal):
        """Updates the GP with new data as well as the current context. Creates
        a model if none exists yet.

        Parameters
        ----------
        goal : ContextualBayesOptAction
            The goal (context variable of the current goal is always pre-ceding
            the function value, i.e., the goal consists of [y_n, c_{n+1}]) sent
            from the client for the most recent experiment.
        """
        if self.x_new is None and self.context is None:
            # The very first function value we obtain from the client is just to
            # trigger the server. At that point, there is no new input point,
            # hence, no need to need to update the model. However, the initial
            # context is already valid.
            self.context = torch.tensor(goal.c_new)
            return

        # Concatenate context and optimization variable
        x = torch.cat((self.x_new, self.context))
        self.data_handler.add_xy(x=x, y=goal.y_new)
        self.prev_context = self.context
        self.context = torch.tensor(goal.c_new)

        # Note: We always create a GP model from scratch when receiving new data.
        # The reason is the following: if the 'set_train_data' method of the GP
        # is used instead, the normalization/standardization of the input/output
        # data is not updated in the GPyTorchModel.
        self.gp = self._initialize_model(*self.data_handler.get_xy())
        self._optimize_model()

    def _initialize_model(self, x, y) -> GPyTorchModel:
        # Kernel for optimization variables
        ad0 = tuple(range(self.input_dim))
        k0 = MaternKernel(active_dims=ad0, lengthscale_prior=GammaPrior(3.0, 6.0))

        # Kernel for context variables
        ad1 = tuple(range(self.input_dim, self.input_dim + self.context_dim))
        k1 = MaternKernel(active_dims=ad1, lengthscale_prior=GammaPrior(3.0, 6.0))

        # Joint kernel is constructed via multiplication
        covar_module = ScaleKernel(k0 * k1, outputscale_prior=GammaPrior(2.0, 0.15))

        # Note: not using input normalization due to weird behaviour
        # See also https://github.com/pytorch/botorch/issues/874
        gp = SingleTaskGP(
            train_X=x,
            train_Y=y,
            outcome_transform=Standardize(m=1),
            covar_module=covar_module,
        )
        return gp

    def _initialize_acqf(self) -> FixedFeatureAcquisitionFunction:
        """Initialize the acquisition function of choice and wrap it with the
        FixedFeatureAcquisitionFunction given the current context.

        Returns
        -------
        FixedFeatureAcquisitionFunction
            An acquisition function of choice with fixed features.
        """
        acq_func = super()._initialize_acqf()
        columns = [i + self.input_dim for i in range(self.context_dim)]
        values = self.context.tolist()
        acq_func_ff = FixedFeatureAcquisitionFunction(
            acq_func, d=self.joint_dim, columns=columns, values=values
        )
        return acq_func_ff

    def _optimize_acqf(
        self, acq_func: AcquisitionFunction, visualize: bool = False
    ) -> Tuple[Tensor, float]:
        """Optimizes the acquisition function with the context variable fixed.

        Note: The debug visualization is turned off for contextual setting.

        Parameters
        ----------
        acq_func : AcquisitionFunction
            The acquisition function to optimize.
        visualize : bool
            Flag if debug visualization should be turned on.

        Returns
        -------
        x_opt : torch.Tensor
            Location of the acquisition function's optimum.
        f_opt : float
            Value of the acquisition function's optimum.
        """
        x_opt, f_opt = super()._optimize_acqf(acq_func, visualize=False)
        if visualize:
            pass
        return x_opt, f_opt

    def _optimize_posterior_mean(self, context=None) -> Tuple[Tensor, float]:
        """Optimizes the posterior mean function with a fixed context variable.

        Instead of implementing this functionality from scratch, simply use the
        exploitative acquisition function with BoTorch's optimization.

        Parameters
        ----------
        context : torch.Tensor, optional
            The context for which to compute the mean's optimum. If none is
            specified, use the last one that was received.

        Returns
        -------
        x_opt : torch.Tensor
            Location of the posterior mean function's optimum.
        f_opt : float
            Value of the posterior mean function's optimum.
        """
        context = context or self.prev_context
        if not isinstance(context, torch.Tensor):
            context = torch.tensor(context)

        columns = [i + self.input_dim for i in range(self.context_dim)]
        values = context.tolist()

        pm = PosteriorMean(model=self.gp, maximize=self.maximize)
        pm_ff = FixedFeatureAcquisitionFunction(pm, self.joint_dim, columns, values)

        x_opt, f_opt = super()._optimize_acqf(pm_ff, visualize=False)
        f_opt = f_opt if self.maximize else -1 * f_opt
        return x_opt, f_opt

    def _check_data_vicinity(self, x1, x2):
        """Returns true if `x1` is close to any point in `x2`.

        Following Binois and Picheny (2019) - https://www.jstatsoft.org/article/view/v089i08
        Check if the proposed point is too close to any existing data points
        to avoid numerical issues. In that case, choose a random point instead.
        """
        xc1 = torch.cat((x1, self.context))
        return super()._check_data_vicinity(xc1, x2)