#!/bin/bash

#SBATCH -p speech-gpu
#SBATCH --constraint=12g
#SBATCH --array=1-3

bash -c "`sed "${SLURM_ARRAY_TASK_ID}q;d" tune_commands_3`"

