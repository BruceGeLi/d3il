import logging
import os

import multiprocessing as mp
import random
from envs.gym_aligning_env.gym_aligning.envs.aligning import Robot_Push_Env

import numpy as np
import torch
import wandb

from simulation.base_sim import BaseSim
from agents.utils.sim_path import sim_framework_path


log = logging.getLogger(__name__)


train_contexts = np.load(sim_framework_path("environments/dataset/data/aligning/train_contexts.pkl"),
                         allow_pickle=True)

test_contexts = np.load(sim_framework_path("environments/dataset/data/aligning/test_contexts.pkl"),
                        allow_pickle=True)


def assign_process_to_cpu(pid, cpus):
    os.sched_setaffinity(pid, cpus)


class Aligning_Sim(BaseSim):
    def __init__(
            self,
            seed: int,
            device: str,
            render: bool,
            n_cores: int = 1,
            n_contexts: int = 30,
            n_trajectories_per_context: int = 1,
            if_vision: bool = False
    ):
        super().__init__(seed, device, render, n_cores, if_vision)

        self.n_contexts = n_contexts
        self.n_trajectories_per_context = n_trajectories_per_context

    def eval_agent(self, agent, contexts, context_ind, mode_encoding, successes, mean_distance, pid, cpu_set,
                   context_id_dict={}):

        # print(os.getpid(), cpu_set)
        assign_process_to_cpu(os.getpid(), cpu_set)

        env = Robot_Push_Env(render=self.render, if_vision=self.if_vision)
        env.start()

        random.seed(pid)
        torch.manual_seed(pid)
        np.random.seed(pid)

        print(f'core {cpu_set} proceeds Context {contexts} with Rollout context_ind {context_ind}')

        for i, context in enumerate(contexts):
            # for i in range(n_trajectories):

            agent.reset()

            # print(f'Context {context} Rollout {i}')
            # training contexts
            # env.manager.set_index(context)
            # obs = env.reset(random=False, context=test_contexts[context])

            # obs = env.reset()
            obs = env.reset(random=False, context=test_contexts[context])

            # test contexts
            # test_context = env.manager.sample()
            # obs = env.reset(random=False, context=test_context)

            if self.if_vision:
                env_state, bp_image, inhand_image = obs
                bp_image = bp_image.transpose((2, 0, 1)) / 255.
                inhand_image = inhand_image.transpose((2, 0, 1)) / 255.

                des_robot_pos = env_state[:3]
                done = False

                while not done:
                    pred_action = agent.predict((bp_image, inhand_image, des_robot_pos), if_vision=self.if_vision)
                    pred_action = pred_action[0] + des_robot_pos

                    pred_action = np.concatenate((pred_action, [0, 1, 0, 0]), axis=0)
                    obs, reward, done, info = env.step(pred_action)

                    des_robot_pos = pred_action[:3]

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
                done = False
                while not done:

                    obs = np.concatenate((pred_action[:3], obs))

                    pred_action = agent.predict(obs)
                    pred_action = pred_action[0] + obs[:3]

                    pred_action = np.concatenate((pred_action, [0, 1, 0, 0]), axis=0)

                    obs, reward, done, info = env.step(pred_action)

            ### add mapping to the context
            ctxt_idx = context_id_dict[context]
            mode_encoding[ctxt_idx, context_ind[i]] = torch.tensor(info['mode'])
            successes[ctxt_idx, context_ind[i]] = torch.tensor(info['success'])
            mean_distance[ctxt_idx, context_ind[i]] = torch.tensor(info['mean_distance'])

    ################################
    # we use multi-process for the simulation
    # n_contexts: the number of different contexts of environment
    # n_trajectories_per_context: test each context for n times, this is mostly used for multi-modal data
    # n_cores: the number of cores used for simulation
    ###############################
    def test_agent(self, agent, cpu_cores=None):

        # log.info('Starting trained model evaluation')

        mode_encoding = torch.zeros([self.n_contexts, self.n_trajectories_per_context]).share_memory_()
        successes = torch.zeros((self.n_contexts, self.n_trajectories_per_context)).share_memory_()
        mean_distance = torch.zeros((self.n_contexts, self.n_trajectories_per_context)).share_memory_()

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
                        "context_ind": context_ind[ind_workload[i]:ind_workload[i+1]],
                        "mode_encoding": mode_encoding,
                        "successes": successes,
                        "mean_distance": mean_distance,
                        "pid": i,
                        "cpu_set": set([int(cpu_cores[i])]),
                        "context_id_dict": context_idx_dict
                    },
                )
                # print("Start {}".format(i))
                p.start()
                p_list.append(p)
            [p.join() for p in p_list]

        else:
            self.eval_agent(agent, contexts, context_ind, mode_encoding, successes, mean_distance, pid=0, cpu_set=set([0]), context_id_dict=context_idx_dict)

        n_modes = 2

        success_rate = torch.mean(successes).item()
        mode_probs = torch.zeros([self.n_contexts, n_modes])
        if n_modes == 1:
            for c in range(self.n_contexts):
                mode_probs[c, :] = torch.tensor(
                    [sum(mode_encoding[c, successes[c, :] == 1] == 0) / self.n_trajectories_per_context])

        elif n_modes == 2:
            for c in range(self.n_contexts):
                mode_probs[c, :] = torch.tensor(
                    [sum(mode_encoding[c, successes[c, :] == 1] == 0) / self.n_trajectories_per_context,
                     sum(mode_encoding[c, successes[c, :] == 1] == 1) / self.n_trajectories_per_context])

        mode_probs /= (mode_probs.sum(1).reshape(-1, 1) + 1e-12)
        # print(f'p(m|c) {mode_probs}')

        entropy = - (mode_probs * torch.log(mode_probs + 1e-12) / torch.log(
            torch.tensor(n_modes))).sum(1).mean()

        # wandb.log({'score': 0.5 * (success_rate + entropy)})
        # wandb.log({'Metrics/successes': success_rate})
        # wandb.log({'Metrics/entropy': entropy})
        # wandb.log({'Metrics/distance': mean_distance.mean().item()})

        # print(f'Mean Distance {mean_distance.mean().item()}')
        # print(f'Successrate {success_rate}')
        # print(f'entropy {entropy}')

        return {'success_rate':success_rate, 'entropy': entropy.item(), 'mean_dist': mean_distance.mean().item()}