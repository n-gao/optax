# Copyright 2019 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Utilities to perform maths on pytrees."""


from collections.abc import Callable
import operator
from typing import Sequence

import chex
import jax
from jax import tree_util as jtu
import jax.numpy as jnp
from optax import tree_utils as otu
from optax._src import base


Shape = base.Shape


class Normal:
  """Normal distribution."""

  def sample(self,
             seed: chex.PRNGKey,
             sample_shape: Shape) -> jax.Array:
    return jax.random.normal(seed, sample_shape)

  def log_prob(self, inputs: jax.Array) -> jax.Array:
    return -0.5 * inputs ** 2


class Gumbel:
  """Gumbel distribution."""

  def sample(self,
             seed: chex.PRNGKey,
             sample_shape: Shape) -> jax.Array:
    return jax.random.gumbel(seed, sample_shape)

  def log_prob(self, inputs: jax.Array) -> jax.Array:
    return -inputs - jnp.exp(-inputs)


def _tree_mean_across(trees: Sequence[chex.ArrayTree]) -> chex.ArrayTree:
  """Mean across a list of trees.

  Args:
    trees: List or tuple of pytrees with the same structure.

  Returns:
    a pytree with the same structure as each tree in ``trees`` with leaves being
    the mean across the trees.

  >>> optax.tree_utils.tree_reduce_mean_across(
  ...   [{'first': [1, 2], 'last': 3},
  ...    {'first': [5, 6], 'last': 7}]
  ...   )
  {'first': [3, 4], 'last': 5}
  """
  mean_fun = lambda x: sum(x) / len(trees)
  return jtu.tree_map(lambda *leaves: mean_fun(leaves), *trees)


def _tree_vmap(fun: Callable[[chex.ArrayTree], chex.ArrayTree],
               trees: Sequence[chex.ArrayTree]) -> chex.ArrayTree:
  """Applies a function to a list of trees, akin to a vmap."""
  tree_def_in = jtu.tree_structure(trees[0])
  has_in_structure = lambda x: jtu.tree_structure(x) == tree_def_in
  return jtu.tree_map(fun, trees, is_leaf=has_in_structure)


def make_perturbed_fun(fun: Callable[[chex.ArrayTree], chex.ArrayTree],
                       num_samples: int = 1000,
                       sigma: float = 0.1,
                       noise=Gumbel()):
  """Turns a function into a differentiable approximation, with perturbations.
  
  For a function `fun`, it creates a proxy `fun_perturb` defined by
  fun_perturb(inputs) = E[fun(inputs + sigma * Z)],
  where Z is sampled from the `noise` sampler. This implements a Monte-Carlo
  estimate.
  
  References:
    [Berthet et al., 2020]: Learning with Differentiable Perturbed Optimizers
    Q. Berthet, M. Blondel, O. Teboul, M. Cuturi, J-P. Vert, F. Bach
    NeurIPS 2020
  
  Args:
    fun: The function to transform into a differentiable function.
      Signature currently supported is from pytree to pytree, whose leaves are
      jax arrays.
    num_samples: an int, the number of perturbed outputs to average over.
    sigma: a float, the scale of the random perturbation.
    noise: a distribution object that must implement a sample function and a
      log-pdf of the desired distribution, similar to :class: Gumbel.
      Default is Gumbel distribution.

  Returns:
    A function with the same signature (and an rng) that can be automatically
    differentiated.
    
  .. warning: The vjp or gradient of the transformed function does not allow
  additional derivations.
  """

  def _compute_residuals(inputs, rng):
    # random noise Zs to be added to inputs
    samples = [
        otu.tree_random_like(rng_, inputs, sampler=noise.sample)
        for rng_ in jax.random.split(rng, num_samples)
    ]
    # creates [inputs + Z_1, ..., inputs + Z_num_samples]
    inputs_pert = _tree_vmap(
        lambda z: otu.tree_add_scalar_mul(inputs, sigma, z), samples
    )
    # applies fun: [fun(inputs + Z_1), ..., fun(inputs + Z_num_samples)]
    outputs_pert = _tree_vmap(fun, inputs_pert)
    return outputs_pert, samples

  @jax.custom_jvp
  def fun_perturb(inputs, rng):
    outputs_pert, _ = _compute_residuals(inputs, rng)
    # averages the perturbed outputs
    return _tree_mean_across(outputs_pert)

  def fun_perturb_jvp(tangent, _, inputs, rng):
    """Computes the jacobian vector product.
    
    Following the method in [Berthet et al. 2020], for a vector `g`, we have
    Jac(fun_perturb)(inputs) * g = 
    E[fun(inputs + sigma * Z) * <grad log_prob(Z), g>].
    This implements a Monte-Carlo estimate
    
    Args:
      tangent: the tangent in the jacobian vector product.
      _: not used.
      inputs: the inputs to the function.
      rng: the random number generator key.
      
    Returns:
      The jacobian vector product.
    """
    outputs_pert, samples = _compute_residuals(inputs, rng)
    sum_log_prob_func = lambda x: jnp.sum(noise.log_prob(x))
    # computes [grad log_prob(Z_1), ... , grad log_prob(Z_num_samples)]
    tree_sum_log_probs = jtu.tree_map(sum_log_prob_func, samples)
    fun_dot_prod = jax.tree_util.tree_map(jnp.dot,
                                          tree_sum_log_probs,
                                          tangent)
    # computes [<grad log_prob(Z_1), g>, .. , <grad log_prob(Z_num_samples), g>]
    list_tree_dot_prods = _tree_vmap(fun_dot_prod, tree_sum_log_probs)
    list_dot_prods = _tree_vmap(lambda x: jnp.sum(jtu.tree_reduce(operator.add,
                                                                  x)),
                                list_tree_dot_prods)
    # computes 1/M * sum_i fun(inputs + sigma * Z_i) <grad log_prob(Z_i), g>
    tangent_out = _tree_mean_across([
        otu.tree_scalar_mul(scalar_dot_prod, output)
        for scalar_dot_prod, output in zip(list_dot_prods, outputs_pert)
    ])
    return tangent_out

  fun_perturb.defjvps(fun_perturb_jvp, None)

  return fun_perturb
