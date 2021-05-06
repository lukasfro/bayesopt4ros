API Documentation
=================

The Bayesian Optimization Server Class
---------------------------------------

.. automodule:: bayesopt_server
.. autoclass:: BayesOptServer
   :members: __init__, execute_callback, run

The Bayesian Optimizer
----------------------

.. automodule:: bayesopt4ros.bayesopt
.. autoclass:: BayesianOptimization
   :members: __init__, from_file, next, update_last_y, n_data, y_best, x_best

Acquisition Functions
---------------------

.. automodule:: bayesopt4ros.acq_func
.. autoclass:: AcquisitionFunction
   :members: __init__, __call__   
.. autoclass:: ExpectedImprovement
   :members: __init__, __call__
.. autoclass:: UpperConfidenceBound
   :members: __init__, __call__

Optimization
------------

.. automodule:: bayesopt4ros.optim
    :members: maximize_restarts, get_anchor_points

Utilities
---------

.. automodule:: bayesopt4ros.util
.. autoclass:: DataHandler
   :members: __init__, get_xy, set_xy, add_xy, normalize_input, denormalize_input, normalize_output, denormalize_output, n_data, x_best, y_best, to_dict

Integration Tests
-----------------

.. automodule:: test_client_python
   :members: forrester_function, three_hump_camel_function
.. autoclass:: ExampleClient
   :members: __init__, request, run
