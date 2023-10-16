"""mpiexec -n 10 python -m multidiff
"""
from typing import Sequence, Union, NamedTuple, Hashable, Optional, Any
from dataclasses import dataclass
import math

import numpy as np
import jax
from jax import numpy as jnp

from mpi4py import MPI
COMM = MPI.COMM_WORLD

class GradDescentResult(NamedTuple):
    loss: jnp.ndarray
    params: jnp.ndarray
    aux: Union[jnp.ndarray, list]


# NOTE: This assumes the entire data is initially loaded in memory
# TODO: A separate function will be needed for loading data from file(s)
# jax.jit
def distribute_data(data):
    rank, nranks = COMM.Get_rank(), COMM.Get_size()
    fullsize = len(data)
    chunksize = math.ceil(fullsize / nranks)
    start = chunksize * rank
    stop = start + chunksize
    return data[start:stop]


def reduce_sum(value, root=None):
    """Returns the sum of `value` across all MPI processes

    Parameters
    ----------
    value : np.ndarray | float
        value input by each MPI process to be summed
    root : int, optional
        rank of the process to receive and sum the values,
        by default None (broadcast result to all ranks)

    Returns
    -------
    np.ndarray | float
        Sum of values given by each process
    """
    return_to_scalar = not hasattr(value, "__len__")
    value = np.asarray(value)
    if root is None:
        # All-to-all sum
        total = np.empty_like(value)
        COMM.Allreduce(value, total, op=MPI.SUM)
    else:
        # All-to-root sum
        total = np.empty_like(value)
        COMM.Reduce(value, total, op=MPI.SUM, root=root)

    if return_to_scalar:
        total = total.tolist()
    return total


def broadcast(value, root=0):
    return COMM.bcast(value)

def simple_grad_descent(loss_func, guess, nsteps, learning_rate,
                        grad_loss_func=None, has_aux=False, **kwargs):
    rank = COMM.Get_rank()
    if grad_loss_func is None:
        loss_and_grad_func = jax.value_and_grad(
            loss_func, has_aux=has_aux, **kwargs)
    else:
        def loss_and_grad_func(params):
            return (loss_func(params), grad_loss_func(params))

    # Create our mpi4jax token with a dummy broadcast
    def loopfunc(state, _x):
        grad, params = state
        params = jnp.asarray(params)

        # Evaluate the loss and gradient at given parameters
        loss, grad = loss_and_grad_func(params)
        if has_aux:
            (loss, aux), grad = loss, grad[0]
        else:
            aux = None
        y = (loss, params, aux)

        # Calculate the next parameters to evaluate (no need to broadcast this)
        params = params - learning_rate * grad
        # params = broadcast(params, root=0)
        state = grad, params
        return state, y

    initstate = (0.0, guess)
    # iterations = jax.lax.scan(loopfunc, initstate, jnp.arange(nsteps), nsteps)[1]
    # loss, params, aux = iterations
    # The below is equivalent, but doesn't JIT the loopfunc, which might be impossible
    ###################################
    loss, params, aux = [], [], []
    for x in range(nsteps):
        initstate, y = loopfunc(initstate, x)
        loss.append(y[0])
        params.append(y[1])
        aux.append(y[2])
    loss = jnp.array(loss)
    params = jnp.array(params)
    try:
        aux = jnp.array(aux)
    except TypeError:
        pass
    ##################################

    return GradDescentResult(loss=loss, params=params, aux=aux)


# Prototypee for a PyTree (i.e., JAX-compatible object) that stores all
# data and logic required to calculate a model prediction and loss
@jax.tree_util.register_pytree_node_class
@dataclass
class MultiDiffOnePointModel:
    dynamic_data: Optional[dict] = None
    static_data: tuple[Hashable, ...] = ()
    loss_func_has_aux: bool = False
    sumstats_func_has_aux: bool = False
    root: Optional[int] = None

    def calc_partial_sumstats_from_params(self, params):
        """Custom method to map parameters to summary statistics"""
        raise NotImplementedError(
            "Subclass must implement `partial_sumstats_func_from_params`")

    def calc_loss_from_sumstats(self, sumstats, sumstats_aux=None):
        """Custom method to map summary statistics to loss"""
        raise NotImplementedError(
            "Subclass must implement `loss_func_from_sumstats`")

    # NOTE: Never jit this method because it uses mpi4py
    def run_grad_descent(self, guess: Sequence[float],
                         nsteps=100,
                         learning_rate=0.1):
        return simple_grad_descent(
            self.calc_loss_from_params,
            guess=guess,
            nsteps=nsteps,
            learning_rate=learning_rate,
            grad_loss_func=self.calc_dloss_dparams,
            has_aux=self.loss_func_has_aux,
        )

    def __post_init__(self):
        # Create auto-diff functions needed for gradient descent
        # NOTE: jacrev might be faster if there are more params than sumstats
        self._jac_sumstats_from_params = jax.jit(jax.jacfwd(
            self.calc_partial_sumstats_from_params,
            has_aux=self.sumstats_func_has_aux
        ))
        self._grad_loss_from_sumstats = jax.jit(jax.grad(
            self.calc_loss_from_sumstats,
            has_aux=self.loss_func_has_aux
        ))

    # sumstats functions
    # NOTE: Never jit this method because it uses mpi4py (when total=True)
    def calc_sumstats_from_params(self, params: Sequence[float], total=True) -> Union[jnp.ndarray, tuple[jnp.ndarray, Any]]:
        result, aux = self.calc_partial_sumstats_from_params(params), None
        if self.sumstats_func_has_aux:
            result, aux = result
        if total:
            result = jnp.asarray(reduce_sum(result, root=self.root))
        result = (result, aux) if self.sumstats_func_has_aux else result
        return result

    # NOTE: Never jit this method because it uses mpi4py (when total=True)
    def calc_dsumstats_dparams(self, params: Sequence[float], total=True) -> Union[jnp.ndarray, tuple[jnp.ndarray, Any]]:
        params = jnp.asarray(params)
        result, aux = self._jac_sumstats_from_params(params), None
        if self.sumstats_func_has_aux:
            result, aux = result
        if total:
            result = jnp.asarray(reduce_sum(result, root=self.root))
        if self.sumstats_func_has_aux:
            result = (result, aux)
        return result

    # loss functions
    # NOTE: Never jit this method because it uses mpi4py
    def calc_loss_from_params(self, params: Sequence[float]) -> Union[jnp.ndarray, tuple[jnp.ndarray, Any]]:
        sumstats = self.calc_sumstats_from_params(params)
        if not self.sumstats_func_has_aux:
            sumstats = (sumstats,)
        return self.calc_loss_from_sumstats(*sumstats)

    @jax.jit
    def calc_dloss_dsumstats(self, sumstats: Sequence[float], sumstats_aux=None) -> Union[jnp.ndarray, tuple[jnp.ndarray, Any]]:
        sumstats = jnp.asarray(sumstats)
        args = (sumstats, sumstats_aux) if self.sumstats_func_has_aux else (sumstats,)
        return self._grad_loss_from_sumstats(*args)

    # NOTE: Never jit this method because it uses mpi4py
    def calc_dloss_dparams(self, params: Sequence[float]) -> Union[jnp.ndarray, tuple[jnp.ndarray, Any]]:
        params = jnp.asarray(params)
        sumstats = self.calc_sumstats_from_params(params)
        dsumstats_dparams = self.calc_dsumstats_dparams(params)
        if self.sumstats_func_has_aux:
            dsumstats_dparams = dsumstats_dparams[0]
        else:
            sumstats = (sumstats,)

        dloss_dsumstats, aux = self.calc_dloss_dsumstats(*sumstats), None
        if self.loss_func_has_aux:
            dloss_dsumstats, aux = dloss_dsumstats

        # Chain rule
        result = jnp.sum(dloss_dsumstats[:, None] * dsumstats_dparams, axis=0)
        if self.loss_func_has_aux:
            result = result, aux
        return result

    # JAX compatibility functions below
    # =================================
    def tree_flatten(self) -> tuple[tuple, dict]:
        children = (  # arrays / dynamic values
            self.dynamic_data,
        )
        aux_data = dict(  # static values
            static_data=self.static_data,
            loss_func_has_aux=self.loss_func_has_aux,
            sumstats_func_has_aux=self.sumstats_func_has_aux,
            root=self.root,
        )
        return (children, aux_data)

    @classmethod
    def tree_unflatten(cls, aux_data: dict, children: tuple):
        return cls(*children, **aux_data)

if __name__ == "__main__":
    # Maybe somehow perform gradient descent if directly executed?
    pass
