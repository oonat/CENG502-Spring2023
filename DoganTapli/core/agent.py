import multiprocessing
from utils.replay_memory import Memory
from utils.replay_memory import Memory_DAP
from utils.torch import *
from uap import query_uap
import math
import time
import os
import numpy as np
from torchvision import transforms as T
os.environ["OMP_NUM_THREADS"] = "1"

state_transformer = T.Compose([
    #T.ToPILImage(),
	  T.ToTensor()
])


def collect_samples(pid, queue, env, policy, custom_reward,
                    mean_action, render, running_state, min_batch_size):
    if pid > 0:
        torch.manual_seed(torch.randint(0, 5000, (1,)) * pid)
        if hasattr(env, 'np_random'):
            env.np_random.seed(env.np_random.randint(5000) * pid)
        if hasattr(env, 'env') and hasattr(env.env, 'np_random'):
            env.env.np_random.seed(env.env.np_random.randint(5000) * pid)
        #if hasattr(env, 'np_random'):
        #    env.np_random.__setstate__(np.random.default_rng(np.random.randint(5000) * pid).__getstate__())
        #if hasattr(env, 'env') and hasattr(env.env, 'np_random'):
        #    env.env.np_random.__setstate__(np.random.default_rng(np.random.randint(5000) * pid).__getstate__())
    log = dict()
    memory = Memory()
    num_steps = 0
    total_reward = 0
    min_reward = 1e6
    max_reward = -1e6
    total_c_reward = 0
    min_c_reward = 1e6
    max_c_reward = -1e6
    num_episodes = 0

    while num_steps < min_batch_size:
        env.unwrapped.np_random=np.random
        state = env.reset()
        if running_state is not None:
            state = running_state(state)
        reward_episode = 0

        for t in range(10000):
            state_var = state_transformer(state.astype(np.float32)).unsqueeze(0)
            #import pdb; pdb.set_trace()
            #state_var = tensor(state).unsqueeze(0).float()
            with torch.no_grad():
                if mean_action:
                    action = policy(state_var)[0][0].numpy()
                else:
                    action = policy.select_action(state_var)[0].numpy()
            action = int(action) if policy.is_disc_action else action.astype(np.float64)
            next_state, reward, done, _ = env.step(action)
            reward_episode += reward
            if running_state is not None:
                next_state = running_state(next_state)

            if custom_reward is not None:
                reward = custom_reward(state, action)
                total_c_reward += reward
                min_c_reward = min(min_c_reward, reward)
                max_c_reward = max(max_c_reward, reward)

            mask = 0 if done else 1

            memory.push(state_var, action, mask, next_state, reward)

            if render:
                env.render()
            if done:
                break

            state = next_state

        # log stats
        num_steps += (t + 1)
        num_episodes += 1
        total_reward += reward_episode
        min_reward = min(min_reward, reward_episode)
        max_reward = max(max_reward, reward_episode)

    log['num_steps'] = num_steps
    log['num_episodes'] = num_episodes
    log['total_reward'] = total_reward
    log['avg_reward'] = total_reward / num_episodes
    log['max_reward'] = max_reward
    log['min_reward'] = min_reward
    if custom_reward is not None:
        log['total_c_reward'] = total_c_reward
        log['avg_c_reward'] = total_c_reward / num_steps
        log['max_c_reward'] = max_c_reward
        log['min_c_reward'] = min_c_reward

    if queue is not None:
        queue.put([pid, memory, log])
    else:
        return memory, log


def merge_log(log_list):
    log = dict()
    log['total_reward'] = sum([x['total_reward'] for x in log_list])
    log['num_episodes'] = sum([x['num_episodes'] for x in log_list])
    log['num_steps'] = sum([x['num_steps'] for x in log_list])
    log['avg_reward'] = log['total_reward'] / log['num_episodes']
    log['max_reward'] = max([x['max_reward'] for x in log_list])
    log['min_reward'] = min([x['min_reward'] for x in log_list])
    if 'total_c_reward' in log_list[0]:
        log['total_c_reward'] = sum([x['total_c_reward'] for x in log_list])
        log['avg_c_reward'] = log['total_c_reward'] / log['num_steps']
        log['max_c_reward'] = max([x['max_c_reward'] for x in log_list])
        log['min_c_reward'] = min([x['min_c_reward'] for x in log_list])

    return log


class Agent:

    def __init__(self, env, policy, device, custom_reward=None, running_state=None, num_threads=1):
        self.env = env
        self.policy = policy
        self.device = device
        self.custom_reward = custom_reward
        self.running_state = running_state
        self.num_threads = num_threads

        
    # Agent is for the attacker network
    # It injects the universal perturbation if the switch head output is greater than 0.5
    # If injected, the victim network decides the step with the perturbed state
    def collect_DAP_samples(self, min_batch_size, victim_net, max_inject, T, render=False):
        # policy is the attack network
        log = dict()
        memory = Memory_DAP() # Memory : state, action, switch, mask, next_state, reward, inj_run_out
        num_steps = 0
        total_reward = 0
        min_reward = 1e6
        max_reward = -1e6
        total_c_reward = 0
        min_c_reward = 1e6
        max_c_reward = -1e6
        num_episodes = 0
        inj_run_out = []

        while num_steps < min_batch_size:
            state = self.env.reset()
            if self.running_state is not None:
                state = self.running_state(state)
            reward_episode = 0
            inj_num = 0
            inj_run_out = -1

            for t in range(T):
                state_var = state_transformer(state.astype(np.float32)).unsqueeze(0)
                with torch.no_grad():
                    victim_action = victim_net(state_var)
                    l_action, switch = self.policy(state_var, victim_action)

                if switch > 0.5 and inj_num < max_inject:
                    inj_num += 1
                    if inj_num == max_inject:
                        inj_run_out = t

                    orig_action = torch.argmax(victim_action)
                    lure_action =  torch.argmax(l_action)

                    perturbation = query_uap(orig_action, lure_action)

                    state_var += perturbation

                with torch.no_grad():
                    action = torch.argmax(victim_net(state_var))

                next_state, reward, done, _ = self.env.step(action)
                reward_episode += reward
                if self.running_state is not None:
                    next_state = self.running_state(next_state)

                if self.custom_reward is not None:
                    reward = self.custom_reward(state, action)
                    total_c_reward += reward
                    min_c_reward = min(min_c_reward, reward)
                    max_c_reward = max(max_c_reward, reward)

                mask = 0 if done else 1

                memory.push(state_var, l_action, switch, mask, next_state, reward, inj_run_out)

                if render:
                    self.env.render()
                if done:
                    break

                state = next_state

            # log stats
            num_steps += (t + 1)
            num_episodes += 1
            total_reward += reward_episode
            min_reward = min(min_reward, reward_episode)
            max_reward = max(max_reward, reward_episode)

        log['num_steps'] = num_steps
        log['num_episodes'] = num_episodes
        log['total_reward'] = total_reward
        log['avg_reward'] = total_reward / num_episodes
        log['max_reward'] = max_reward
        log['min_reward'] = min_reward
        if self.custom_reward is not None:
            log['total_c_reward'] = total_c_reward
            log['avg_c_reward'] = total_c_reward / num_steps
            log['max_c_reward'] = max_c_reward
            log['min_c_reward'] = min_c_reward

        return memory, log


    def collect_samples(self, min_batch_size, mean_action=False, render=False):
        t_start = time.time()
        to_device(torch.device('cpu'), self.policy)
        thread_batch_size = int(math.floor(min_batch_size / self.num_threads))
        queue = multiprocessing.Queue()
        workers = []

        for i in range(self.num_threads-1):
            worker_args = (i+1, queue, self.env, self.policy, self.custom_reward, mean_action,
                           False, self.running_state, thread_batch_size)
            workers.append(multiprocessing.Process(target=collect_samples, args=worker_args))
        for worker in workers:
            worker.start()

        memory, log = collect_samples(0, None, self.env, self.policy, self.custom_reward, mean_action,
                                      render, self.running_state, thread_batch_size)

        worker_logs = [None] * len(workers)
        worker_memories = [None] * len(workers)
        for _ in workers:
            pid, worker_memory, worker_log = queue.get()
            worker_memories[pid - 1] = worker_memory
            worker_logs[pid - 1] = worker_log
        for worker_memory in worker_memories:
            memory.append(worker_memory)
        batch = memory.sample()
        if self.num_threads > 1:
            log_list = [log] + worker_logs
            log = merge_log(log_list)
        to_device(self.device, self.policy)
        t_end = time.time()
        log['sample_time'] = t_end - t_start
        log['action_mean'] = np.mean(np.vstack(batch.action), axis=0)
        log['action_min'] = np.min(np.vstack(batch.action), axis=0)
        log['action_max'] = np.max(np.vstack(batch.action), axis=0)
        return batch, log
