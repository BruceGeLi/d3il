python run.py --config-name=avoiding_config \
              --multirun seed=0,1,2,3,4,5 \
              agents=beso_agent \
              agent_name=beso \
              window_size=1 \
              group=avoiding_beso_seeds \
              simulation.n_cores=40 \
              simulation.n_trajectories=480 \
              agents.num_sampling_steps=4 \
              agents.sigma_min=0.1 \
              agents.sigma_max=1