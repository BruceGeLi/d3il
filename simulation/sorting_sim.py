import logging
import os

# import multiprocessing as mp
import torch.multiprocessing as mp
import random

from envs.gym_sorting_env.gym_sorting.envs.sorting import Sorting_Env
import numpy as np
import torch
import wandb

from simulation.base_sim import BaseSim
from agents.utils.sim_path import sim_framework_path

log = logging.getLogger(__name__)


def assign_process_to_cpu(pid, cpus):
    os.sched_setaffinity(pid, cpus)


class Sorting_Sim(BaseSim):
    def __init__(
            self,
            seed: int,
            device: str,
            render: bool,
            n_cores: int = 1,
            n_contexts: int = 30,
            n_trajectories_per_context: int = 1,
            num_box: int = 2,
            if_vision: bool = False,
            max_steps_per_episode: int = 500
    ):
        super().__init__(seed, device, render, n_cores, if_vision)

        self.n_contexts = n_contexts
        self.n_trajectories_per_context = n_trajectories_per_context

        self.max_steps_per_episode = max_steps_per_episode

        self.num_box = num_box

        self.test_contexts = np.load(sim_framework_path("environments/dataset/data/sorting/" + str(num_box) + "_test_contexts.pkl"),
                                     allow_pickle=True)

        self.modes = np.load(sim_framework_path("environments/dataset/data/sorting/" + str(num_box) + "_mode_prob.pkl"), allow_pickle=True)

        self.mode_keys = np.array(list(self.modes.keys()))
        self.n_mode = len(self.mode_keys)

        mode_encoding = []

        for i in range(self.n_mode):
            mode_encoding.append(self.modes[self.mode_keys[i]])

        self.mode_encoding = torch.tensor(mode_encoding)

    def eval_agent(self, agent, contexts, context_ind, mode_encoding, successes, num_complete, pid, cpu_set, context_id_dict={}):

        # print(os.getpid(), cpu_set)
        assign_process_to_cpu(os.getpid(), cpu_set)

        env = Sorting_Env(max_steps_per_episode=self.max_steps_per_episode, render=self.render, num_boxes=self.num_box, if_vision=self.if_vision)
        env.start()

        random.seed(pid)
        torch.manual_seed(pid)
        np.random.seed(pid)

        print(f'core {cpu_set} proceeds Context {contexts} with Rollout context_ind {context_ind}')

        for i, context in contexts:

            agent.reset()

            obs = env.reset(random=False, context=self.test_contexts[context], if_vision=self.if_vision)

            # obs = env.reset()

            # test contexts
            # test_context = env.manager.sample()
            # obs = env.reset(random=False, context=test_context)

            # rollout for image_based policy
            if self.if_vision:

                robot_pos, bp_image, inhand_image = obs
                bp_image = bp_image.transpose((2, 0, 1)) / 255.
                inhand_image = inhand_image.transpose((2, 0, 1)) / 255.

                fixed_z = env.robot_state()[2:]
                des_robot_pos = robot_pos
                done = False

                while not done:

                    pred_action = agent.predict((bp_image, inhand_image, des_robot_pos), if_vision=self.if_vision)
                    pred_action = pred_action[0] + des_robot_pos

                    pred_action = np.concatenate((pred_action, fixed_z, [0, 1, 0, 0]), axis=0)
                    obs, reward, done, info = env.step(pred_action)

                    des_robot_pos = pred_action[:2]

                    robot_pos, bp_image, inhand_image = obs

                    # cv2.imshow('0', bp_image)
                    # cv2.waitKey(1)
                    #
                    # cv2.imshow('1', inhand_image)
                    # cv2.waitKey(1)

                    bp_image = bp_image.transpose((2, 0, 1)) / 255.
                    inhand_image = inhand_image.transpose((2, 0, 1)) / 255.

            else:

                pred_action = env.robot_state()
                fixed_z = pred_action[2:]
                done = False
                while not done:
                    obs = np.concatenate((pred_action[:2], obs))

                    pred_action = agent.predict(obs)
                    pred_action = pred_action[0] + obs[:2]

                    pred_action = np.concatenate((pred_action, fixed_z, [0, 1, 0, 0]), axis=0)

                    obs, reward, done, info = env.step(pred_action)

            ctxt_idx = context_id_dict[context]
            mode_encoding[ctxt_idx, i] = torch.tensor(info['mode'])
            successes[ctxt_idx, i] = torch.tensor(info['success'])
            num_complete[ctxt_idx, i] = torch.tensor(len(info['min_inds']))

    ################################
    # we use multi-process for the simulation
    # n_contexts: the number of different contexts of environment
    # n_trajectories_per_context: test each context for n times, this is mostly used for multi-modal data
    # n_cores: the number of cores used for simulation
    ###############################
    def test_agent(self, agent, cpu_cores=None):

        log.info('Starting trained model evaluation')

        mode_encoding = torch.zeros([self.n_contexts, self.n_trajectories_per_context]).share_memory_()
        successes = torch.zeros((self.n_contexts, self.n_trajectories_per_context)).share_memory_()
        mean_distance = torch.zeros((self.n_contexts, self.n_trajectories_per_context)).share_memory_()
        num_complete = torch.zeros((self.n_contexts, self.n_trajectories_per_context)).share_memory_()

        #####################################################################
        ## get assignment to cores
        ####################################################################
        self.n_cores = len(cpu_cores) if cpu_cores is not None else 10

        contexts = np.random.randint(0, 60, self.n_contexts) if self.n_contexts != 60 else np.arange(60)
        context_idx_dict = {c: i for i, c in enumerate(contexts)}

        contexts = np.repeat(contexts, self.n_trajectories_per_context)

        context_ind = np.arange(self.n_trajectories_per_context)
        context_ind = np.tile(context_ind, self.n_contexts)

        repeat_nums = (self.n_contexts * self.n_trajectories_per_context) // self.n_cores
        repeat_res = (self.n_contexts * self.n_trajectories_per_context) % self.n_cores

        workload_array = np.ones([self.n_cores], dtype=int)
        workload_array[:repeat_res] += repeat_nums
        workload_array[repeat_res:] = repeat_nums

        assert np.sum(workload_array) == len(contexts)

        ind_workload = np.cumsum(workload_array)
        ind_workload = np.concatenate(([0], ind_workload))
        ########################################################################

        ctx = mp.get_context('spawn')

        p_list = []
        if self.n_cores > 1:
            for i in range(self.n_cores):
                p = ctx.Process(
                    target=self.eval_agent,
                    kwargs={
                        "agent": agent,
                        "contexts": contexts[ind_workload[i]:ind_workload[i+1]],
                        "context_ind": context_ind[ind_workload[i]:ind_workload[i + 1]],
                        "mode_encoding": mode_encoding,
                        "successes": successes,
                        "num_complete": num_complete,
                        "pid": i,
                        "cpu_set": set([int(cpu_cores[i])]), #set(cpu_set[i:i+1]),
                        "context_idx_dict": context_idx_dict,
                    },
                )
                # print("Start {}".format(i))
                p.start()
                p_list.append(p)
            [p.join() for p in p_list]

        else:
            self.eval_agent(agent, contexts, context_ind, mode_encoding, successes, num_complete, 0, cpu_set=set([0]), context_id_dict=context_idx_dict)

        success_rate = torch.mean(successes).item()
        mode_probs = torch.zeros([self.n_contexts, self.n_mode])

        for c in range(self.n_contexts):

            for num in range(self.n_mode):
                mode_probs[c, num] = torch.tensor(
                    [sum(mode_encoding[c, successes[c, :] == 1] == self.mode_keys[num]) / self.n_trajectories_per_context])

        mode_probs /= (mode_probs.sum(1).reshape(-1, 1) + 1e-12)
        # print(f'p(m|c) {mode_probs}')

        mode_probs = mode_probs[torch.nonzero(mode_probs.sum(1), as_tuple=True)[0]]

        if success_rate == 0:
            entropy = 0
        else:
            entropy = - (mode_probs * torch.log(mode_probs + 1e-12) / torch.log(torch.tensor(self.n_mode))).sum(1).mean()
        # log_ = (mode_probs * torch.log(self.mode_encoding + 1e-12) / torch.log(torch.tensor(self.n_mode))).sum(1).mean()

        # KL = - entropy - log_

        # wandb.log({'score': (success_rate - KL)})
        # wandb.log({'Metrics/successes': success_rate})
        # wandb.log({'Metrics/KL': KL})
        # wandb.log({'Metrics/entropy': entropy})
        # wandb.log({'Metrics/distance': mean_distance.mean().item()})
        # mean_distance = mean_distance.mean().item()
        num_complete = num_complete.mean().item()

        # print(f'Mean Distance {mean_distance.mean().item()}')
        # print(f'Successrate {success_rate}')
        # print(f'entropy {entropy}')
        # print(f'num_complete {num_complete}')
        # print(f'KL {KL}')

        return success_rate, entropy, num_complete