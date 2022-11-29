import numpy as np
import matplotlib.pyplot as plt
import jax
import jax.numpy as jnp
import os

### Some convenient function for doing grid search of the hyperparameters ###


def search_wrapper(ess_function, amin, amax, epsmin, epsmax):
    show= False

    A = jnp.logspace(np.log10(amin), np.log10(amax), 6)

    epsilon = jnp.logspace(np.log10(epsmin), np.log10(epsmax), 6)

    results1 = search_step(ess_function, A, epsilon)

    if show:
        plt.figure(figsize= (15, 10))
        plt.subplot(1, 2, 1)
    ess, i, j = visualize(results1, A, epsilon, show = show)

    if (i == 0) or (i == 5) or (j == 0) or (j == 5):
        if show:
            plt.show()
        print('warning bounds')
        return ess, A[i], epsilon[j]


    else:
        A = jnp.logspace(np.log10(A[i-1]), np.log10(A[i+1]), 6)
        epsilon = jnp.logspace(np.log10(epsilon[j-1]), np.log10(epsilon[j+1]), 6)
        results2 = search_step(ess_function, A, epsilon)

        if show:
            plt.subplot(1, 2, 2)
        ess, i, j = visualize(results2, A, epsilon, show=show)
        if show:
            plt.show()

    return ess, A[i], epsilon[j]



def search_step(ess_function, A, epsilon):
    return jax.vmap(lambda a: jax.pmap(lambda e: ess_function(a, e))(epsilon))(A)


def visualize(ess_arr, A, epsilon, show):

    I = np.argmax(ess_arr)
    eps_best = epsilon[I % (len(epsilon))]
    alpha_best = A[I // len(epsilon)]
    ess_best = np.max(ess_arr)
    print(ess_best)

    if show:
        ax = plt.gca()
        cax = ax.matshow(ess_arr)
        plt.colorbar(cax)
        plt.title(r'ESS = {0} ($\alpha$ = {1}, $\epsilon$ = {2})'.format(np.round(ess_best, 4), *np.round([alpha_best, eps_best], 2)))


        ax.set_xticklabels([''] + [str(e)[:4] for e in epsilon])
        ax.set_yticklabels([''] + [str(a)[:4] for a in A])
        ax.xaxis.set_label_position('top')
        ax.set_xlabel(r'$\epsilon$')
        ax.set_ylabel(r'$\alpha$')
        ax.invert_yaxis()

    return ess_best, I // len(epsilon), I % (len(epsilon))




def search_wrapper_1d(ess_function, epsmin, epsmax):

    epsilon = jnp.logspace(np.log10(epsmin), np.log10(epsmax), 6)

    results1 = jax.pmap(ess_function)(epsilon)

    j = jnp.argmax(results1)
    ess = results1[j]
    eps= epsilon[j]

    plt.figure(figsize= (15, 10))
    plt.plot(epsilon, results1, 'o', color = 'black')
    plt.xlabel(r'$\epsilon$')
    plt.ylabel('ESS')

    if (j == 0) or (j == 5):
        plt.title(r'ESS = {0}, $\epsilon$ = {1}'.format(np.round(ess, 4), np.round(epsilon[j], 2)))
        plt.show()

    else:
        epsilon = jnp.logspace(np.log10(epsilon[j-1]), np.log10(epsilon[j+1]), 6)
        results2 = jax.pmap(ess_function)(epsilon)
        j = jnp.argmax(results2)
        ess_new = results2[j]
        if ess_new > ess:
            ess = ess_new
            eps = epsilon[j]

        plt.plot(epsilon, results2, 'o', color='black')
        plt.title(r'ESS = {0}, $\epsilon$ = {1}'.format(np.round(ess, 4), np.round(epsilon[j], 2)))
        plt.show()

    print(ess)
    return ess, eps
