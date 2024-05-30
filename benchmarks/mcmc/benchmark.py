import sys
sys.path.append('./')
from collections import defaultdict
from functools import partial
import math
import operator
import os
import pprint
from statistics import mean, median
import jax
import jax.numpy as jnp
import pandas as pd
import scipy

from blackjax.adaptation.mclmc_adaptation import MCLMCAdaptationState

os.environ["XLA_FLAGS"] = '--xla_force_host_platform_device_count=' + str(128)
num_cores = jax.local_device_count()
# print(num_cores, jax.lib.xla_bridge.get_backend().platform)

import itertools

import numpy as np

import blackjax
from benchmarks.mcmc.sampling_algorithms import integrator_order, run_mclmc, run_adjusted_mclmc, run_nuts, samplers
from benchmarks.mcmc.inference_models import Banana, Brownian, Funnel, GermanCredit, IllConditionedGaussian, ItemResponseTheory, MixedLogit, StandardNormal, StochasticVolatility, models
from blackjax.mcmc.integrators import generate_euclidean_integrator, generate_isokinetic_integrator, isokinetic_mclachlan, mclachlan_coefficients, omelyan_coefficients, velocity_verlet, velocity_verlet_coefficients, yoshida_coefficients
from blackjax.mcmc.adjusted_mclmc import rescale
from blackjax.util import run_inference_algorithm





def get_num_latents(target):
  return target.ndims
#   return int(sum(map(np.prod, list(jax.tree_flatten(target.event_shape)[0]))))


def err(f_true, var_f, contract):
    """Computes the error b^2 = (f - f_true)^2 / var_f
        Args:
            f: E_sampler[f(x)], can be a vector
            f_true: E_true[f(x)]
            var_f: Var_true[f(x)]
            contract: how to combine a vector f in a single number, can be for example jnp.average or jnp.max
            
        Returns:
            contract(b^2)
    """    
    
    return jax.vmap(lambda f: contract(jnp.square(f - f_true) / var_f))



def grads_to_low_error(err_t, grad_evals_per_step= 1, low_error= 0.01):
    """Uses the error of the expectation values to compute the effective sample size neff
        b^2 = 1/neff"""
    
    cutoff_reached = err_t[-1] < low_error
    return find_crossing(err_t, low_error) * grad_evals_per_step, cutoff_reached
    
        
def calculate_ess(err_t, grad_evals_per_step, neff= 100):
    
    grads_to_low, cutoff_reached = grads_to_low_error(err_t, grad_evals_per_step, 1./neff)
    
    return (neff / grads_to_low) * cutoff_reached, grads_to_low*(1/cutoff_reached), cutoff_reached


def find_crossing(array, cutoff):
    """the smallest M such that array[m] < cutoff for all m > M"""

    b = array > cutoff
    indices = jnp.argwhere(b)
    if indices.shape[0] == 0:
        print("\n\n\nNO CROSSING FOUND!!!\n\n\n", array, cutoff)
        return 1

    return jnp.max(indices)+1


def cumulative_avg(samples):
    return jnp.cumsum(samples, axis = 0) / jnp.arange(1, samples.shape[0] + 1)[:, None]


def gridsearch_tune(key, iterations, grid_size, model, sampler, batch, num_steps, center_L, center_step_size, contract):
    results = defaultdict(float)
    converged = False
    keys = jax.random.split(key, iterations+1)
    for i in range(iterations):
        print(f"EPOCH {i}")
        width = 2
        step_sizes = np.logspace(np.log10(center_step_size/width), np.log10(center_step_size*width), grid_size)
        Ls = np.logspace(np.log10(center_L/2), np.log10(center_L*2),grid_size)
        # print(list(itertools.product(step_sizes , Ls)))

        grid_keys = jax.random.split(keys[i], grid_size^2)
        print(f"center step size {center_step_size}, center L {center_L}")
        for j, (step_size, L) in enumerate(itertools.product(step_sizes , Ls)):
            ess, grad_calls_until_convergence, _ , _, _ = benchmark_chains(model, sampler(step_size=step_size, L=L), grid_keys[j], n=num_steps, batch = batch, contract=contract)
            results[(step_size, L)] = (ess, grad_calls_until_convergence)

        best_ess, best_grads, (step_size, L) = max([(results[r][0], results[r][1], r) for r in results], key=operator.itemgetter(0))
        # raise Exception
        print(f"best params on iteration {i} are stepsize {step_size} and L {L} with Grad Calls until Convergence {best_grads}")
        if L==center_L and step_size==center_step_size:
            print("converged")
            converged = True
            break
        else:
            center_L, center_step_size = L, step_size

    pprint.pp(results)
        # print(f"best params on iteration {i} are stepsize {step_size} and L {L} with Grad Calls until Convergence {best_grads}")
        # print(f"L from ESS (0.4 * step_size/ESS): {0.4 * step_size/best_ess}")
    return center_L, center_step_size, converged


def run_adjusted_mclmc_no_tuning(initial_state, coefficients, step_size, L, sqrt_diag_cov):

    def s(logdensity_fn, num_steps, initial_position, transform, key):

        integrator = generate_isokinetic_integrator(coefficients)

        num_steps_per_traj = L/step_size
        alg = blackjax.mcmc.adjusted_mclmc.adjusted_mclmc(
        logdensity_fn=logdensity_fn,
        step_size=step_size,
        integration_steps_fn = lambda k : jnp.ceil(jax.random.uniform(k) * rescale(num_steps_per_traj)) ,
        integrator=integrator,
        sqrt_diag_cov=sqrt_diag_cov,
        )

        _, out, info = run_inference_algorithm(
        rng_key=key,
        initial_state=initial_state,
        inference_algorithm=alg,
        num_steps=num_steps, 
        transform=lambda x: transform(x.position), 
        progress_bar=True)

        return out, MCLMCAdaptationState(L=L, step_size=step_size, sqrt_diag_cov=sqrt_diag_cov), num_steps_per_traj * calls_per_integrator_step(coefficients), info.acceptance_rate.mean(), None, jnp.array([0])

    return s

def benchmark_chains(model, sampler, key, n=10000, batch=None, contract = jnp.average,):

    pvmap = jax.pmap

    # def pvmap(f):
    #     def f(arr):
    #         return arr
    #         print(arr.shape,"shape")
    #         print(arr)
    #         arr = arr.reshape(128, -1)
    #         out = jax.vmap(jax.vmap(f), in_axes=0)(arr)
    #         return out.flatten()
    #     return f
    
    d = get_num_latents(model)
    if batch is None:
        batch = np.ceil(1000 / d).astype(int)
    key, init_key = jax.random.split(key, 2)
    keys = jax.random.split(key, batch)

    init_keys = jax.random.split(init_key, batch)
    init_pos = pvmap(model.sample_init)(init_keys) # [batch_size, dim_model]

    # samples, params, avg_num_steps_per_traj = jax.pmap(lambda pos, key: sampler(model.logdensity_fn, n, pos, model.transform, key))(init_pos, keys)
    ex2_empirical, params, grad_calls_per_traj, acceptance_rate, step_size_over_da, final_da = pvmap(lambda pos, key: sampler(logdensity_fn=model.logdensity_fn, num_steps=n, initial_position= pos,transform= model.transform, key=key))(init_pos, keys)
    avg_grad_calls_per_traj = jnp.nanmean(grad_calls_per_traj, axis=0)
    try:
        print(jnp.nanmean(params.step_size,axis=0), jnp.nanmean(params.L,axis=0))
    except: pass
    
    full = lambda arr : err(model.E_x2, model.Var_x2, contract)(arr)
    err_t = pvmap(full)(ex2_empirical)

    # outs = [calculate_ess(b, grad_evals_per_step=avg_grad_calls_per_traj) for b in err_t]
    # # print(outs[:10])
    # esses = [i[0].item() for i in outs if not math.isnan(i[0].item())]
    # grad_calls = [i[1].item() for i in outs if not math.isnan(i[1].item())]
    # return(mean(esses), mean(grad_calls))
    # print(final_da.mean(), "final da")



    err_t_median = jnp.median(err_t, axis=0)
    # import matplotlib.pyplot as plt
    # plt.plot(np.arange(1, 1+ len(err_t_median))* 2, err_t_median, color= 'teal', lw = 3)
    # plt.xlabel('gradient evaluations')
    # plt.ylabel('average second moment error')
    # plt.xscale('log')
    # plt.yscale('log')
    # plt.savefig('brownian.png')
    # plt.close()
    esses, grad_calls, _ = calculate_ess(err_t_median, grad_evals_per_step=avg_grad_calls_per_traj)
    return esses, grad_calls, params, jnp.mean(acceptance_rate, axis=0), step_size_over_da




# def run_benchmarks(batch_size):

#     results = defaultdict(tuple)
#     for variables in itertools.product(
#         # ["adjusted_mclmc", "nuts", "mclmc", ], 
#         ["adjusted_mclmc"], 
#         # [StandardNormal(d) for d in np.ceil(np.logspace(np.log10(10), np.log10(10000), 10)).astype(int)],
#         [Brownian()],
#         # [Brownian()],
#         # [Brownian()],
#         # [velocity_verlet_coefficients, mclachlan_coefficients, yoshida_coefficients, omelyan_coefficients], 
#         [mclachlan_coefficients], 
#         ):

#         sampler, model, coefficients = variables
#         num_chains = batch_size#1 + batch_size//model.ndims


#         num_steps = 100000

#         sampler, model, coefficients = variables
#         num_chains = batch_size # 1 + batch_size//model.ndims

#         # print(f"\nModel: {model.name,model.ndims}, Sampler: {sampler}\n Coefficients: {coefficients}\nNumber of chains {num_chains}",) 

#         contract = jnp.max

#         key = jax.random.PRNGKey(11)
#         for i in range(1):
#             key1, key = jax.random.split(key)
#             ess, grad_calls, params , acceptance_rate, step_size_over_da = benchmark_chains(model, partial(samplers[sampler], integrator_type=coefficients, frac_tune1=0.1, frac_tune2=0.0, frac_tune3=0.0),key1, n=num_steps, batch=num_chains, contract=contract)

#             # print(f"step size over da {step_size_over_da.shape} \n\n\n\n")
#             jax.numpy.save(f"step_size_over_da.npy", step_size_over_da.mean(axis=0))
#             jax.numpy.save(f"acceptance.npy", acceptance_rate)


#             # print(f"grads to low bias: {grad_calls}")
#             # print(f"acceptance rate is {acceptance_rate, acceptance_rate.mean()}")

#             results[((model.name, model.ndims), sampler, coefficients, "standard", acceptance_rate.mean().item(), params.L.mean().item(), params.step_size.mean().item(), num_chains, num_steps, contract)] = ess.item()
#             print(ess.item())
#             # results[(model.name, model.ndims, "nuts", 0., 0., (coeffs), "standard", acceptance_rate)]

            
#     # print(results)
            

#     df = pd.Series(results).reset_index()
#     df.columns = ["model", "sampler", "integrator", "tuning", "acc rate", "L", "stepsize", "num_chains", "num steps", "contraction", "ESS"] 
#     # df.result = df.result.apply(lambda x: x[0].item())
#     # df.model = df.model.apply(lambda x: x[1])
#     df.to_csv("results_simple.csv", index=False)

#     return results

# vary step_size
def run_benchmarks_step_size(batch_size):

    results = defaultdict(tuple)
    for variables in itertools.product(
        # ["adjusted_mclmc", "nuts", "mclmc", ], 
        ["adjusted_mclmc"], 
        # [StandardNormal(d) for d in np.ceil(np.logspace(np.log10(10), np.log10(10000), 10)).astype(int)],
        [StandardNormal(10)],
        # [Brownian()],
        # [Brownian()],
        # [velocity_verlet_coefficients, mclachlan_coefficients, yoshida_coefficients, omelyan_coefficients], 
        [mclachlan_coefficients], 
        ):



        num_steps = 10000

        sampler, model, coefficients = variables
        num_chains = batch_size # 1 + batch_size//model.ndims

        # print(f"\nModel: {model.name,model.ndims}, Sampler: {sampler}\n Coefficients: {coefficients}\nNumber of chains {num_chains}",) 

        contract = jnp.average

        center = 6.534974
        key = jax.random.PRNGKey(11)
        for step_size in np.linspace(center-1,center+1, 41):
        # for L in np.linspace(1, 10, 41):
            key1, key2, key3, key = jax.random.split(key, 4)
            initial_position = model.sample_init(key2)
            initial_state = blackjax.mcmc.adjusted_mclmc.init(
            position=initial_position, logdensity_fn=model.logdensity_fn, random_generator_arg=key3)
            ess, grad_calls, params , acceptance_rate, _ = benchmark_chains(model, run_adjusted_mclmc_no_tuning(initial_state=initial_state, integrator_type=mclachlan_coefficients, step_size=step_size, L= 5*step_size, sqrt_diag_cov=1.),key1, n=num_steps, batch=num_chains, contract=contract)

            # print(f"step size over da {step_size_over_da.shape} \n\n\n\n")
            # jax.numpy.save(f"step_size_over_da.npy", step_size_over_da.mean(axis=0))
            # jax.numpy.save(f"acceptance.npy_{step_size}", acceptance_rate)


            # print(f"grads to low bias: {grad_calls}")
            # print(f"acceptance rate is {acceptance_rate, acceptance_rate.mean()}")

            results[((model.name, model.ndims), sampler, (coefficients), "standard", acceptance_rate.mean().item(), params.L.mean().item(), params.step_size.mean().item(), num_chains, num_steps, contract)] = ess.item()
            # results[(model.name, model.ndims, "nuts", 0., 0., (coeffs), "standard", acceptance_rate)]

            
    # print(results)
            

    df = pd.Series(results).reset_index()
    df.columns = ["model", "sampler", "integrator", "tuning", "acc rate", "L", "stepsize", "num_chains", "num steps", "contraction", "ESS"] 
    # df.result = df.result.apply(lambda x: x[0].item())
    # df.model = df.model.apply(lambda x: x[1])
    df.to_csv("results_step_size.csv", index=False)

    return results



def benchmark_mhmchmc(batch_size):

    key0, key1, key2, key3 = jax.random.split(jax.random.PRNGKey(5), 4)

    # coefficients = [yoshida_coefficients, mclachlan_coefficients, velocity_verlet_coefficients, omelyan_coefficients]
    # coefficients = [mclachlan_coefficients, velocity_verlet_coefficients]
    integrators = ["mclachlan", "velocity_verlet"]
    for model in models:
        results = defaultdict(tuple)
        for preconditioning, integrator_type in itertools.product([True, False], integrators):
        
            num_chains = batch_size # 1 + batch_size//model.ndims
            print(f"NUMBER OF CHAINS for {model.name} and adjusted_mclmc is {num_chains}")
            num_steps = models[model]["adjusted_mclmc"]
            print(f"NUMBER OF STEPS for {model.name} and MHCMLMC is {num_steps}")

            ####### run mclmc with standard tuning

            contract = jnp.max
            

            ess, grad_calls, params , _, step_size_over_da = benchmark_chains(
                model,
                partial(run_mclmc,integrator_type=integrator_type, preconditioning=preconditioning),
                key0,
                n=num_steps,
                batch=num_chains,
                contract=contract)
            results[(model.name, model.ndims, "mclmc", params.L.mean().item(), params.step_size.mean().item(), (integrator_type), "standard", 1., preconditioning)] = ess.item()
            print(f'mclmc with tuning ESS {ess}')


            ####### run adjusted_mclmc with standard tuning 
            for target_acc_rate in [0.65, 0.9]:
                # coeffs = mclachlan_coefficients
                ess, grad_calls, params , acceptance_rate, _ = benchmark_chains(
                    model, 
                    partial(run_adjusted_mclmc, target_acc_rate=target_acc_rate, integrator_type=integrator_type, frac_tune1=0.1, frac_tune2=0.1, frac_tune3=0.0, preconditioning=preconditioning), 
                    key1, 
                    n=num_steps, 
                    batch=num_chains, 
                    contract=contract)
                results[(model.name, model.ndims, "mhmchmc"+str(target_acc_rate), jnp.nanmean(params.L).item(), jnp.nanmean(params.step_size).item(), (integrator_type), "standard", acceptance_rate.mean().item(), preconditioning)] = ess.item()
                print(f'adjusted_mclmc with tuning ESS {ess}')
                
                # integrator_type = mclachlan_coefficients
                ess, grad_calls, params , acceptance_rate, _ = benchmark_chains(
                    model, 
                    partial(run_adjusted_mclmc, target_acc_rate=target_acc_rate,integrator_type=integrator_type, frac_tune1=0.1, frac_tune2=0.1, frac_tune3=0.1, preconditioning=preconditioning), 
                    key1, 
                    n=num_steps, 
                    batch=num_chains, 
                    contract=contract)
                results[(model.name, model.ndims, "mhmchmc:st3"+str(target_acc_rate), jnp.nanmean(params.L).item(), jnp.nanmean(params.step_size).item(), (integrator_type), "standard", acceptance_rate.mean().item(), preconditioning)] = ess.item()
                print(f'adjusted_mclmc with tuning ESS {ess}')

            if False:
                ####### run adjusted_mclmc with standard tuning + grid search

                init_pos_key, init_key, tune_key, grid_key, bench_key = jax.random.split(key2, 5)
                initial_position = model.sample_init(init_pos_key)

                initial_state = blackjax.mcmc.adjusted_mclmc.init(
                position=initial_position, logdensity_fn=model.logdensity_fn, random_generator_arg=init_key
                )

                kernel = lambda rng_key, state, avg_num_integration_steps, step_size, sqrt_diag_cov: blackjax.mcmc.adjusted_mclmc.build_kernel(
                            integrator=generate_isokinetic_integrator(integrator_type),
                            integration_steps_fn = lambda k : jnp.ceil(jax.random.uniform(k) * rescale(avg_num_integration_steps)),
                            sqrt_diag_cov=sqrt_diag_cov,
                        )(
                            rng_key=rng_key, 
                            state=state, 
                            step_size=step_size, 
                            logdensity_fn=model.logdensity_fn)

                (
                    state,
                    blackjax_adjusted_mclmc_sampler_params,
                    _, _
                ) = blackjax.adaptation.mclmc_adaptation.adjusted_mclmc_find_L_and_step_size(
                    mclmc_kernel=kernel,
                    num_steps=num_steps,
                    state=initial_state,
                    rng_key=tune_key,
                    target=target_acceptance_rate_of_order[integrator_order(integrator_type)],
                    frac_tune1=0.1,
                    frac_tune2=0.1,
                    frac_tune3=0.0,
                    diagonal_preconditioning=False
                )

                print(f"target acceptance rate {target_acceptance_rate_of_order[integrator_order(integrator_type)]}")
                print(f"params after initial tuning are L={blackjax_adjusted_mclmc_sampler_params.L}, step_size={blackjax_adjusted_mclmc_sampler_params.step_size}")


                L, step_size, convergence = gridsearch_tune(grid_key, iterations=10, contract=contract, grid_size=5, model=model, sampler=partial(run_adjusted_mclmc_no_tuning, integrator_type=integrator_type, initial_state=state, sqrt_diag_cov=1.), batch=num_chains, num_steps=num_steps, center_L=blackjax_adjusted_mclmc_sampler_params.L, center_step_size=blackjax_adjusted_mclmc_sampler_params.step_size)
                # print(f"params after grid tuning are L={L}, step_size={step_size}")


                ess, grad_calls, _ , acceptance_rate, _ = benchmark_chains(model, run_adjusted_mclmc_no_tuning(integrator_type=integrator_type, L=L, step_size=step_size, initial_state=state, sqrt_diag_cov=1.),bench_key, n=num_steps, batch=num_chains, contract=contract)

                print(f"grads to low bias: {grad_calls}")

                results[(model.name, model.ndims, "mhmchmc:grid", L.item(), step_size.item(), (integrator_type), f"gridsearch:{convergence}", acceptance_rate.mean().item()), True] = ess.item()

            ####### run nuts

            # integrator_type = velocity_verlet_coefficients
            ess, grad_calls, _ , acceptance_rate, _ = benchmark_chains(model, partial(run_nuts,integrator_type=integrator_type, preconditioning=preconditioning),key3, n=models[model]["nuts"], batch=num_chains, contract=contract)
            results[(model.name, model.ndims, "nuts", 0., 0., (integrator_type), "standard", acceptance_rate.mean().item(), preconditioning)] = ess.item()
            





                
            print(results)
                

        df = pd.Series(results).reset_index()
        df.columns = ["model", "dims", "sampler", "L", "step_size", "integrator", "tuning", "acc_rate", "preconditioning", "ESS"] 
        # df.result = df.result.apply(lambda x: x[0].item())
        # df.model = df.model.apply(lambda x: x[1])
        df.to_csv(f"results{model.name}.csv", index=False)

    return results

# TODO: not updated to new code yet!
def benchmark_omelyan(batch_size):


    key = jax.random.PRNGKey(2)
    results = defaultdict(tuple)
    for variables in itertools.product(
        # ["adjusted_mclmc", "nuts", "mclmc", ], 
        ["mhmchmc"], 
        [StandardNormal(d) for d in np.ceil(np.logspace(np.log10(10), np.log10(1000), 4)).astype(int)],
        # [StandardNormal(d) for d in np.ceil(np.logspace(np.log10(10), np.log10(10000), 5)).astype(int)],
        # models,
        # [velocity_verlet_coefficients, mclachlan_coefficients, yoshida_coefficients, omelyan_coefficients], 
        [mclachlan_coefficients, omelyan_coefficients], 
        ):


        sampler, model, coefficients = variables

        # num_chains = 1 + batch_size//model.ndims
        num_chains = batch_size

        current_key, key = jax.random.split(key) 
        init_pos_key, init_key, tune_key, bench_key, grid_key = jax.random.split(current_key, 5)

        # num_steps = models[model][sampler]

        num_steps = 1000


        initial_position = model.sample_init(init_pos_key)

        initial_state = blackjax.mcmc.adjusted_mclmc.init(
        position=initial_position, logdensity_fn=model.logdensity_fn, random_generator_arg=init_key
        )

     
        kernel = lambda rng_key, state, avg_num_integration_steps, step_size, sqrt_diag_cov: blackjax.mcmc.adjusted_mclmc.build_kernel(
                    integrator=generate_isokinetic_integrator(coefficients),
                    integration_steps_fn = lambda k : jnp.ceil(jax.random.uniform(k) * rescale(avg_num_integration_steps)),
                    sqrt_diag_cov=sqrt_diag_cov,
                )(
                    rng_key=rng_key, 
                    state=state, 
                    step_size=step_size, 
                    logdensity_fn=model.logdensity_fn)

        (
            state,
            blackjax_adjusted_mclmc_sampler_params,
            _, _
        ) = blackjax.adaptation.mclmc_adaptation.adjusted_mclmc_find_L_and_step_size(
            mclmc_kernel=kernel,
            num_steps=num_steps,
            state=initial_state,
            rng_key=tune_key,
            target=target_acceptance_rate_of_order[integrator_order(coefficients)],
            frac_tune1=0.1,
            frac_tune2=0.1,
            # frac_tune3=0.1,
            diagonal_preconditioning=False
        )

        print(f"\nModel: {model.name,model.ndims}, Sampler: {sampler}\n Coefficients: {coefficients}\nNumber of chains {num_chains}",) 
        print(f"params after initial tuning are L={blackjax_adjusted_mclmc_sampler_params.L}, step_size={blackjax_adjusted_mclmc_sampler_params.step_size}")

        # ess, grad_calls, _ , _ = benchmark_chains(model, run_adjusted_mclmc_no_tuning(integrator_type=coefficients, L=blackjax_mclmc_sampler_params.L, step_size=blackjax_mclmc_sampler_params.step_size, sqrt_diag_cov=1.),bench_key_pre_grid, n=num_steps, batch=num_chains, contract=jnp.average)

        # results[((model.name, model.ndims), sampler, (coefficients), "without grid search")] = (ess, grad_calls) 

        L, step_size, converged = gridsearch_tune(grid_key, iterations=10, contract=jnp.average, grid_size=5, model=model, sampler=partial(run_adjusted_mclmc_no_tuning, integrator_type=coefficients, initial_state=state, sqrt_diag_cov=1.), batch=num_chains, num_steps=num_steps, center_L=blackjax_adjusted_mclmc_sampler_params.L, center_step_size=blackjax_adjusted_mclmc_sampler_params.step_size)
        print(f"params after grid tuning are L={L}, step_size={step_size}")


        ess, grad_calls, _ , _, _ = benchmark_chains(model, run_adjusted_mclmc_no_tuning(integrator_type=coefficients, L=L, step_size=step_size, sqrt_diag_cov=1., initial_state=state),bench_key, n=num_steps, batch=num_chains, contract=jnp.average)

        print(f"grads to low bias: {grad_calls}")

        results[(model.name, model.ndims, sampler, (coefficients), converged, L.item(), step_size.item())] = ess.item()

    df = pd.Series(results).reset_index()
    df.columns = ["model", "dims", "sampler", "integrator", "convergence", "L", "step_size", "ESS"] 
    # df.result = df.result.apply(lambda x: x[0].item())
    # df.model = df.model.apply(lambda x: x[1])
    df.to_csv("omelyan.csv", index=False)


def run_benchmarks_simple():

    # sampler = run_adjusted_mclmc
    sampler = run_mclmc
    # model = IllConditionedGaussian(10,100) 
    # model = Brownian()
    # model = StandardNormal(10)
    model = Banana()
    integrator_type = "mclachlan"
    contract = jnp.max # how we average across dimensions
    num_steps = 1000
    num_chains = 1
    for i in range(1):
        key1 = jax.random.PRNGKey(i)

        for preconditioning in [False]:

            ess, grad_calls, params , acceptance_rate, step_size_over_da = benchmark_chains(model, partial(sampler, integrator_type=integrator_type, preconditioning=preconditioning, 
                        # target_acc_rate=0.95, frac_tune3=0.1
                        ),key1, n=num_steps, batch=num_chains, contract=contract)

            print(f"Effective Sample Size (ESS) of {model.name} with preconditioning set to {preconditioning} is {ess}")


if __name__ == "__main__":


    run_benchmarks_simple()

    # benchmark_mhmchmc(batch_size=1)
    # run_benchmarks(128)
    # run_benchmarks_step_size(128)
    # benchmark_omelyan(128)
    # run_benchmarks(128)
    # benchmark_omelyan(10)
    # print("4")



