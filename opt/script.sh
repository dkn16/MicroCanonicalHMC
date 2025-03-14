#!/bin/bash

#SBATCH -A m4031
#SBATCH -N 1
#SBATCH -C cpu
#SBATCH --image=reubenharry/cosmo:1.0
#SBATCH -q debug
#SBATCH -c 128
#SBATCH -J MCLMC_g0.01
#SBATCH -t 00:30:00
#SBATCH --mail-type=end,fail
#SBATCH --mail-user=dkn20@berkeley.edu


# load environment
module load conda
conda activate diffuser

cd /pscratch/sd/d/dkn16/MicroCanonicalHMC/

shifter python3 -m opt.main 0 0.5 1 7
#python3 -m bias.main 0 5 1 8

#python3 -m bias.main 1 5 5 8
#python3 -m bias.main 1 5 20 8
