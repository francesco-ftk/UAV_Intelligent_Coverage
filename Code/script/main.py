import sys

sys.path.append('/home/fantechi/tesi/5G_UAV_Intelligent_Coverage/5G_UAV_ICoverage')

import time
import gymnasium as gym
import torch
import torch.optim as optim
import torch.nn as nn
import numpy as np
import random
import torch.nn.functional as F
import wandb

from gym_cruising.memory.replay_memory import ReplayMemory, Transition
from gym_cruising.neural_network.MLP_policy_net import MLPPolicyNet
from gym_cruising.neural_network.deep_Q_net import DeepQNet, DoubleDeepQNet
from gym_cruising.neural_network.transformer_encoder_decoder import TransformerEncoderDecoder

UAV_NUMBER = 0

TRAIN = False
BATCH_SIZE = 256  # is the number of transitions random sampled from the replay buffer
LEARNING_RATE = 1e-4  # is the learning rate of the Adam optimizer, should decrease (1e-5)
BETA = 0.005  # is the update rate of the target network
GAMMA = 0.99  # Discount Factor
sigma_policy = 0.4  # Standard deviation of noise for policy actor actions on current state
sigma = 0.2  # Standard deviation of noise for target policy actions on next states
c = 0.2  # Clipping bound of noise
policy_delay = 2  # delay for policy and target nets update
start_steps = 20000

MAX_SPEED_UAV = 55.6  # m/s - about 20 Km/h x 10 steps

time_steps_done = 0
optimization_steps = 3

BEST_VALIDATION = 0.0
MAX_LAST_RCR = 0.0
EMBEDDED_DIM = 32

# if gpu is to be used
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
print("DEVICE:", device)

if TRAIN:

    wandb.init(project="mixlast")

    env = gym.make('gym_cruising:Cruising-v0', render_mode='rgb_array', track_id=2)
    env.action_space.seed(42)

    # ACTOR POLICY NET policy
    transformer_policy = TransformerEncoderDecoder(embed_dim=EMBEDDED_DIM).to(device)
    mlp_policy = MLPPolicyNet(token_dim=EMBEDDED_DIM).to(device)

    # CRITIC Q NET policy
    deep_Q_net_policy = DoubleDeepQNet(state_dim=EMBEDDED_DIM).to(device)

    # COMMENT FOR INITIAL TRAINING -> CURRICULUM LEARNING
    # PATH_TRANSFORMER = '../neural_network/bestTransformer.pth'
    # transformer_policy.load_state_dict(torch.load(PATH_TRANSFORMER))
    # PATH_MLP_POLICY = '../neural_network/bestMLP.pth'
    # mlp_policy.load_state_dict(torch.load(PATH_MLP_POLICY))
    # PATH_DEEP_Q = '../neural_network/bestDeepQ.pth'
    # deep_Q_net_policy.load_state_dict(torch.load(PATH_DEEP_Q))

    # ACTOR POLICY NET target
    transformer_target = TransformerEncoderDecoder(embed_dim=EMBEDDED_DIM).to(device)
    mlp_target = MLPPolicyNet(token_dim=EMBEDDED_DIM).to(device)

    # CRITIC Q NET target
    deep_Q_net_target = DoubleDeepQNet(state_dim=EMBEDDED_DIM).to(device)

    # set target parameters equal to main parameters
    transformer_target.load_state_dict(transformer_policy.state_dict())
    mlp_target.load_state_dict(mlp_policy.state_dict())
    deep_Q_net_target.load_state_dict(deep_Q_net_policy.state_dict())

    optimizer_transformer = optim.Adam(transformer_policy.parameters(), lr=LEARNING_RATE, weight_decay=1e-5)
    optimizer_mlp = optim.Adam(mlp_policy.parameters(), lr=LEARNING_RATE, weight_decay=1e-5)
    optimizer_deep_Q = optim.Adam(deep_Q_net_policy.parameters(), lr=LEARNING_RATE, weight_decay=1e-5)

    replay_buffer_uniform = ReplayMemory(100000)
    replay_buffer_clustered = ReplayMemory(100000)


    def select_actions_epsilon(state, uav_number):
        global time_steps_done
        uav_info, connected_gu_positions = np.split(state, [uav_number * 2], axis=0)
        uav_info = uav_info.reshape(uav_number, 4)
        uav_info = torch.from_numpy(uav_info).float().to(device)
        connected_gu_positions = torch.from_numpy(connected_gu_positions).float().to(device)
        action = []
        with torch.no_grad():
            tokens = transformer_policy(connected_gu_positions.unsqueeze(0), uav_info.unsqueeze(0)).squeeze(0)
        time_steps_done += 1
        for i in range(uav_number):
            if time_steps_done < start_steps:
                output = np.random.uniform(low=-1.0, high=1.0, size=2)
                output = output * MAX_SPEED_UAV
                action.append(output)
            else:
                with torch.no_grad():
                    # return action according to MLP [vx, vy] + epsilon noise
                    output = mlp_policy(tokens[i])
                    output = output + (torch.randn(2) * sigma_policy).to(
                        device)
                    output = torch.clip(output, -1.0, 1.0)
                    output = output.cpu().numpy().reshape(2)
                    output = output * MAX_SPEED_UAV
                    action.append(output)
        return action


    def optimize_model():
        global BATCH_SIZE

        if len(replay_buffer_uniform) < 5000 or len(replay_buffer_clustered) < 5000:
            return

        transitions_uniform = replay_buffer_uniform.sample(int(BATCH_SIZE / 2))
        # This converts batch-arrays of Transitions to Transition of batch-arrays.
        batch_uniform = Transition(*zip(*transitions_uniform))

        transitions_clustered = replay_buffer_clustered.sample(int(BATCH_SIZE / 2))
        # This converts batch-arrays of Transitions to Transition of batch-arrays.
        batch_clustered = Transition(*zip(*transitions_clustered))

        states_batch = batch_uniform.states + batch_clustered.states
        actions_batch = batch_uniform.actions + batch_clustered.actions
        actions_batch = tuple(
            [torch.tensor(array, dtype=torch.float32) for array in sublist] for sublist in actions_batch)
        rewards_batch = batch_uniform.rewards + batch_clustered.rewards
        rewards_batch = torch.tensor(rewards_batch, dtype=torch.float32).unsqueeze(1).to(device)
        next_states_batch = batch_uniform.next_states + batch_clustered.next_states
        terminated_batch = batch_uniform.terminated + batch_clustered.terminated
        terminated_batch = torch.tensor(terminated_batch, dtype=torch.float32).unsqueeze(1).to(device)

        # prepare the batch of states
        state_uav_info_batch = tuple(np.split(array, [optimization_steps * 2], axis=0)[0] for array in states_batch)
        state_uav_info_batch = tuple(array.reshape(optimization_steps, 4) for array in state_uav_info_batch)
        state_uav_info_batch = tuple(torch.from_numpy(array).float().to(device) for array in state_uav_info_batch)
        state_uav_info_batch = torch.stack(state_uav_info_batch)

        state_connected_gu_positions_batch = tuple(
            np.split(array, [optimization_steps * 2], axis=0)[1] for array in states_batch)
        state_connected_gu_positions_batch = tuple(
            torch.from_numpy(array).float().to(device) for array in state_connected_gu_positions_batch)
        max_len = max(tensor.shape[0] for tensor in state_connected_gu_positions_batch)
        padded_tensors = []
        for tensor in state_connected_gu_positions_batch:
            padding = (0, 0, 0, max_len - tensor.shape[0])  # Aggiungi il padding alla fine della prima dimensione
            padded_tensor = F.pad(tensor, padding, "constant", 0)  # Padding con 0
            padded_tensors.append(padded_tensor)
        state_connected_gu_positions_batch = torch.stack(padded_tensors)

        # prepare the batch of next states
        next_state_uav_info_batch = tuple(
            np.split(array, [optimization_steps * 2], axis=0)[0] for array in next_states_batch)
        next_state_uav_info_batch = tuple(array.reshape(optimization_steps, 4) for array in next_state_uav_info_batch)
        next_state_uav_info_batch = tuple(
            torch.from_numpy(array).float().to(device) for array in next_state_uav_info_batch)
        next_state_uav_info_batch = torch.stack(next_state_uav_info_batch)

        next_state_connected_gu_positions_batch = tuple(
            np.split(array, [optimization_steps * 2], axis=0)[1] for array in next_states_batch)
        next_state_connected_gu_positions_batch = tuple(
            torch.from_numpy(array).float().to(device) for array in next_state_connected_gu_positions_batch)
        max_len = max(tensor.shape[0] for tensor in next_state_connected_gu_positions_batch)
        padded_tensors = []
        for tensor in next_state_connected_gu_positions_batch:
            padding = (0, 0, 0, max_len - tensor.shape[0])  # Aggiungi il padding alla fine della prima dimensione
            padded_tensor = F.pad(tensor, padding, "constant", 0)  # Padding con 0
            padded_tensors.append(padded_tensor)
        next_state_connected_gu_positions_batch = torch.stack(padded_tensors)

        # get tokens from batch of states and next states
        with torch.no_grad():
            tokens_batch_next_states_target = transformer_target(next_state_connected_gu_positions_batch,
                                                                 next_state_uav_info_batch)  # [BATCH_SIZE, optimization_steps, EMBEDDED_DIM]
            tokens_batch_states_target = transformer_target(state_connected_gu_positions_batch,
                                                            state_uav_info_batch)  # [BATCH_SIZE, optimization_steps, EMBEDDED_DIM]
        tokens_batch_states = transformer_policy(state_connected_gu_positions_batch,
                                                 state_uav_info_batch)  # [BATCH_SIZE, optimization_steps, EMBEDDED_DIM]

        loss_Q = 0.0
        loss_policy = 0.0
        loss_transformer = 0.0

        for i in range(optimization_steps):
            # index mask for not padded current uav in batch
            index_mask = [
                k for k, lista in enumerate(actions_batch)
                if not torch.equal(lista[i], torch.tensor([100., 100.]))
            ]

            masked_batch_size = len(index_mask)

            # UPDATE Q-FUNCTION
            with torch.no_grad():
                # slice i-th UAV's tokens [masked_batch_size, 1, EMBEDDED_DIM]
                current_batch_tensor_tokens_next_states_target = tokens_batch_next_states_target[index_mask, i:i + 1,
                                                                 :].squeeze(
                    1)
                output_batch = mlp_target(current_batch_tensor_tokens_next_states_target)
                # noise generation for target next states actions according to N(0,sigma)
                noise = (torch.randn((masked_batch_size, 2)) * sigma).to(device)
                # Clipping of noise
                clipped_noise = torch.clip(noise, -c, c)
                output_batch = torch.clip(output_batch + clipped_noise, -1.0, 1.0)
                output_batch = output_batch * MAX_SPEED_UAV  # actions batch for UAV i-th [masked_batch_size, 2]
                Q1_values_batch, Q2_values_batch = deep_Q_net_target(current_batch_tensor_tokens_next_states_target,
                                                                     output_batch)
                current_uav_rewards = rewards_batch[..., i]
                current_y_batch = current_uav_rewards[index_mask] + GAMMA * (
                            1.0 - terminated_batch[index_mask]) * torch.min(Q1_values_batch,
                                                                            Q2_values_batch)
            # slice i-th UAV's tokens [masked_batch_size, 1, EMBEDDED_DIM]
            current_batch_tensor_tokens_states = tokens_batch_states[index_mask, i:i + 1, :].squeeze(1)
            # Concatenate i-th UAV's actions along the batch size [masked_batch_size, 2]
            current_batch_actions = torch.cat(
                [action[i].unsqueeze(0) for action in actions_batch],
                dim=0).to(device)
            Q1_values_batch, Q2_values_batch = deep_Q_net_policy(current_batch_tensor_tokens_states,
                                                                 current_batch_actions[index_mask])
            # criterion = nn.MSELoss()
            criterion = torch.nn.HuberLoss()
            # Optimize Deep Q Net
            loss_Q += criterion(Q1_values_batch, current_y_batch) + criterion(Q2_values_batch, current_y_batch)

            criterion = nn.MSELoss()
            # UPDATE POLICY
            # slice i-th UAV's tokens [masked_batch_size, 1, EMBEDDED_DIM]
            current_batch_tensor_tokens_states_target = tokens_batch_states_target[index_mask, i:i + 1, :].squeeze(1)
            output_batch = mlp_policy(current_batch_tensor_tokens_states_target)
            output_batch = output_batch * MAX_SPEED_UAV  # actions batch for UAV i-th [masked_batch_size, 2]
            Q1_values_batch, Q2_values_batch = deep_Q_net_policy(current_batch_tensor_tokens_states_target,
                                                                 output_batch)
            loss_policy += -Q1_values_batch.mean()
            loss_transformer += criterion(current_batch_tensor_tokens_states, current_batch_tensor_tokens_states_target)

        # log metrics to wandb
        wandb.log({"loss_Q": loss_Q, "loss_policy": loss_policy, "loss_transformer": loss_transformer})

        optimizer_deep_Q.zero_grad()
        optimizer_transformer.zero_grad()
        loss_Q.backward()
        torch.nn.utils.clip_grad_norm_(deep_Q_net_policy.parameters(), 5)  # clip_grad_value_
        torch.nn.utils.clip_grad_norm_(transformer_policy.parameters(), 5)  # clip_grad_value_
        optimizer_deep_Q.step()
        # Optimize Transformer Net
        optimizer_transformer.step()

        if time_steps_done % policy_delay == 0:
            # Optimize Policy Net MLP
            optimizer_mlp.zero_grad()
            loss_policy.backward()
            torch.nn.utils.clip_grad_norm_(mlp_policy.parameters(), 5)  # clip_grad_value_
            optimizer_mlp.step()

            soft_update_target_networks()


    def soft_update_target_networks():
        # Soft update of the target network's weights
        # Q' = beta * Q + (1 - beta) * Q'
        target_net_state_dict = transformer_target.state_dict()
        policy_net_state_dict = transformer_policy.state_dict()
        for key in policy_net_state_dict:
            target_net_state_dict[key] = policy_net_state_dict[key] * BETA + target_net_state_dict[key] * (
                    1 - BETA)
        transformer_target.load_state_dict(target_net_state_dict)

        target_net_state_dict = mlp_target.state_dict()
        policy_net_state_dict = mlp_policy.state_dict()
        for key in policy_net_state_dict:
            target_net_state_dict[key] = policy_net_state_dict[key] * BETA + target_net_state_dict[key] * (
                    1 - BETA)
        mlp_target.load_state_dict(target_net_state_dict)

        target_net_state_dict = deep_Q_net_target.state_dict()
        policy_net_state_dict = deep_Q_net_policy.state_dict()
        for key in policy_net_state_dict:
            target_net_state_dict[key] = policy_net_state_dict[key] * BETA + target_net_state_dict[key] * (
                    1 - BETA)
        deep_Q_net_target.load_state_dict(target_net_state_dict)


    def get_uniform_options():
        global UAV_NUMBER

        if UAV_NUMBER == 1:
            uav_number = 1
            starting_gu_number = random.randint(30, 60)
        elif UAV_NUMBER == 2:
            uav_number = 2
            starting_gu_number = random.randint(50, 100)
        else:
            uav_number = 3
            starting_gu_number = random.randint(60, 120)

        return ({
            "uav": uav_number,
            "gu": starting_gu_number,
            "clustered": 0,
            "clusters_number": 0,
            "variance": 0
        })


    def get_clustered_options():
        global UAV_NUMBER

        variance = random.randint(70000, 100000)

        if UAV_NUMBER == 1:
            clusters_number = random.randint(1, 2)
            starting_gu_number = 30 * clusters_number
            uav_number = 1
        elif UAV_NUMBER == 2:
            clusters_number = random.randint(2, 4)
            starting_gu_number = 25 * clusters_number
            uav_number = 2
        else:
            clusters_number = random.randint(3, 6)
            starting_gu_number = 20 * clusters_number
            uav_number = 3

        return ({
            "uav": uav_number,
            "gu": starting_gu_number,
            "clustered": 1,
            "clusters_number": clusters_number,
            "variance": variance
        })


    def get_set_up():
        global UAV_NUMBER

        sample = random.random()
        if sample > 0.3:
            options = get_clustered_options()
        else:
            options = get_uniform_options()

        if UAV_NUMBER == 3:
            UAV_NUMBER = 0
        else:
            UAV_NUMBER += 1

        return options


    def select_actions(state, uav_numebr):
        uav_info, connected_gu_positions = np.split(state, [uav_numebr * 2], axis=0)
        uav_info = uav_info.reshape(uav_numebr, 4)
        uav_info = torch.from_numpy(uav_info).float().to(device)
        connected_gu_positions = torch.from_numpy(connected_gu_positions).float().to(device)
        action = []
        with torch.no_grad():
            tokens = transformer_policy(connected_gu_positions.unsqueeze(0), uav_info.unsqueeze(0)).squeeze(0)
        for i in range(uav_numebr):
            with torch.no_grad():
                # return action according to MLP [vx, vy]
                output = mlp_policy(tokens[i])
                output = output.cpu().numpy().reshape(2)
                output = output * MAX_SPEED_UAV
                action.append(output)
        return action


    def add_padding(state, next_state, actions, reward, uav_number):
        padding = np.array([[0., 0.]])
        action_padding = [100., 100.]
        if uav_number == 1:
            for i in range(2, 6):
                state = np.insert(state, i, padding, axis=0)
                next_state = np.insert(next_state, i, padding, axis=0)
            actions.append(action_padding)
            actions.append(action_padding)
            reward.append(0.)
            reward.append(0.)
        if uav_number == 2:
            for i in range(4, 6):
                state = np.insert(state, i, padding, axis=0)
                next_state = np.insert(next_state, i, padding, axis=0)
            actions.append(action_padding)
            reward.append(0.)
        return state, next_state, actions, reward


    def validate():
        global BEST_VALIDATION
        global MAX_LAST_RCR
        reward_sum_uniform = 0.0
        reward_sum_clustered = 0.0
        sum_last_rcr = 0.0
        options = ({
                       "uav": 1,
                       "gu": 30,
                       "clustered": 1,
                       "clusters_number": 1,
                       "variance": 100000
                   },
                   {
                       "uav": 2,
                       "gu": 60,
                       "clustered": 1,
                       "clusters_number": 2,
                       "variance": 100000
                   },
                   {
                       "uav": 3,
                       "gu": 90,
                       "clustered": 1,
                       "clusters_number": 3,
                       "variance": 100000
                   })

        seeds = [42, 751, 853]
        for i, seed in enumerate(seeds):
            state, info = env.reset(seed=seed, options=options[i])
            steps = 1
            uav_number = options[i]["uav"]
            while True:
                actions = select_actions(state, uav_number)
                next_state, reward, terminated, truncated, info = env.step(actions)
                reward_sum_clustered += sum(reward)

                if steps == 300:
                    truncated = True
                done = terminated or truncated

                state = next_state
                steps += 1

                if done:
                    sum_last_rcr += float(info['RCR'])
                    break

        wandb.log({"reward_clustered": reward_sum_clustered})

        options = ({
                       "uav": 1,
                       "gu": 30,
                       "clustered": 0,
                       "clusters_number": 0,
                       "variance": 0
                   },
                   {
                       "uav": 2,
                       "gu": 60,
                       "clustered": 0,
                       "clusters_number": 0,
                       "variance": 0
                   },
                   {
                       "uav": 3,
                       "gu": 90,
                       "clustered": 0,
                       "clusters_number": 0,
                       "variance": 0
                   })

        seeds = [54321, 1181, 3475]
        for i, seed in enumerate(seeds):
            state, info = env.reset(seed=seed, options=options[i])
            steps = 1
            uav_number = options[i]["uav"]
            while True:
                actions = select_actions(state, uav_number)
                next_state, reward, terminated, truncated, info = env.step(actions)
                reward_sum_uniform += sum(reward)

                if steps == 300:
                    truncated = True
                done = terminated or truncated

                state = next_state
                steps += 1

                if done:
                    sum_last_rcr += float(info['RCR'])
                    break

        wandb.log({"reward_uniform": reward_sum_uniform})
        wandb.log({"max_rcr": sum_last_rcr})

        total_reward = reward_sum_clustered + reward_sum_uniform

        if total_reward > BEST_VALIDATION:
            BEST_VALIDATION = total_reward
            # save the best validation nets
            torch.save(transformer_policy.state_dict(), '../neural_network/rewardTransformer.pth')
            torch.save(mlp_policy.state_dict(), '../neural_network/rewardMLP.pth')
            torch.save(deep_Q_net_policy.state_dict(), '../neural_network/rewardDeepQ.pth')

        if sum_last_rcr > MAX_LAST_RCR:
            MAX_LAST_RCR = sum_last_rcr
            # save the best validation nets
            torch.save(transformer_policy.state_dict(), '../neural_network/maxTransformer.pth')
            torch.save(mlp_policy.state_dict(), '../neural_network/maxMLP.pth')
            torch.save(deep_Q_net_policy.state_dict(), '../neural_network/maxDeepQ.pth')


    if torch.cuda.is_available():
        num_episodes = 8000
    else:
        num_episodes = 100

    print("START UAV COOPERATIVE COVERAGE TRAINING")

    for i_episode in range(0, num_episodes, 1):
        print("Episode: ", i_episode)
        options = get_set_up()
        state, info = env.reset(seed=int(time.perf_counter()), options=options)
        steps = 1
        while True:
            actions = select_actions_epsilon(state, options['uav'])
            next_state, reward, terminated, truncated, _ = env.step(actions)

            if steps == 300:
                truncated = True
            done = terminated or truncated

            # Store the transition in memory
            state_padding, next_state_padding, actions_padding, reward_padding = add_padding(state, next_state, actions,
                                                                                             reward,
                                                                                             options['uav'])
            if options['clustered'] == 0:
                replay_buffer_uniform.push(state_padding, actions_padding, next_state_padding, reward_padding,
                                           int(terminated))
            else:
                replay_buffer_clustered.push(state_padding, actions_padding, next_state_padding, reward_padding,
                                             int(terminated))

            # Move to the next state
            state = next_state
            # Perform one step of the optimization
            optimize_model()
            steps += 1

            if done:
                break

        if len(replay_buffer_uniform) >= 5000 and len(replay_buffer_clustered) >= 5000:
            validate()

    # save the nets
    torch.save(transformer_policy.state_dict(), '../neural_network/lastTransformer.pth')
    torch.save(mlp_policy.state_dict(), '../neural_network/lastMLP.pth')
    torch.save(deep_Q_net_policy.state_dict(), '../neural_network/lastDeepQ.pth')

    wandb.finish()
    env.close()
    print('TRAINING COMPLETE')

else:

    def select_actions(state, uav_numebr):
        uav_info, connected_gu_positions = np.split(state, [uav_numebr * 2], axis=0)
        uav_info = uav_info.reshape(uav_numebr, 4)
        uav_info = torch.from_numpy(uav_info).float().to(device)
        connected_gu_positions = torch.from_numpy(connected_gu_positions).float().to(device)
        action = []
        with torch.no_grad():
            tokens = transformer_policy(connected_gu_positions.unsqueeze(0), uav_info.unsqueeze(0)).squeeze(0)
        for i in range(uav_numebr):
            with torch.no_grad():
                # return action according to MLP [vx, vy]
                output = mlp_policy(tokens[i])
                output = output.cpu().numpy().reshape(2)
                output = output * MAX_SPEED_UAV
                action.append(output)
        return action

    # for numerical test
    env = gym.make('gym_cruising:Cruising-v0', render_mode='rgb_array', track_id=2)

    env.action_space.seed(42)

    # ACTOR POLICY NET policy
    transformer_policy = TransformerEncoderDecoder(embed_dim=EMBEDDED_DIM).to(device)
    mlp_policy = MLPPolicyNet(token_dim=EMBEDDED_DIM).to(device)

    PATH_TRANSFORMER = './neural_network/last1Transformer.pth'
    transformer_policy.load_state_dict(torch.load(PATH_TRANSFORMER))
    PATH_MLP_POLICY = './neural_network/last1MLP.pth'
    mlp_policy.load_state_dict(torch.load(PATH_MLP_POLICY))

    options = ({
        "uav": 3,
        "gu": 120,
        "clustered": 0,
        "clusters_number": 3,
        "variance": 100000
    })

    seeds = [5522, 6004, 9648, 8707, 5930, 7411, 8761, 6748, 283, 4880, 7541, 2423, 9652, 4469, 3508, 8969, 8222, 6413,
             3133, 273, 1431, 9688, 6940, 9998, 7097, 1130, 7583, 4018, 116, 1626, 9579, 2641, 8602, 3335, 7980, 3434,
             1553, 4961, 2024, 2834, 6610, 979, 9405, 4866, 7437, 3827, 3735, 2038, 1360, 5202, 4870, 1945, 382, 7101,
             2402, 7235, 8967, 2315, 5955, 4300, 1775, 8136, 1050, 6385, 1068, 5451, 9772, 2331, 6174, 4393, 4873, 7296,
             1780, 5299, 4919, 625, 87, 2240, 2815, 5020, 43, 211, 17, 1243, 97, 23, 57, 1111, 2013, 571, 1729,
             333, 907, 1025, 621162, 513527, 268574, 233097, 342217, 310673]
    tot_rewards = []
    terminanted = 0
    for j, seed in enumerate(seeds):
        print("Test ", str(j))
        state, info = env.reset(seed=seed, options=options)
        steps = 1
        uav_number = options["uav"]
        while True:
            actions = select_actions(state, uav_number)
            next_state, reward, terminated, truncated, info = env.step(actions)

            if steps == 300:
                truncated = True
            done = terminated or truncated

            if terminated:
                terminated+=1

            state = next_state
            steps += 1

            if done:
                tot_rewards.append(float(info['RCR']))
                break

        env.close()

    print("Mean reward: ", sum(tot_rewards) / len(tot_rewards))
    print("Terminato ", terminated)

# LAST1 NET

# 3 uniform 120 ->  Mean reward:  0.791644105584146  100 semi
# 3 uniform 240 ->  Mean reward:  0.8061526873027318 100 semi
# 3 uniform 120 ->  Mean reward:  0.7950495023560501 100 semi speed GU 27.7 m/s

# 3 clustered 120 3 -> Mean reward:  0.7445944392852687  100000
# 3 clustered 240 3 -> Mean reward:  0.7658873702391636  100000
# 3 clustered 120 3 -> Mean reward:  0.7634008765823469  100000 speed GU 27.7 m/s

# 3 clustered 120 6 -> Mean reward:  0.7427700369506306  100000
# 3 clustered 240 6 -> Mean reward:  0.7932372470608483  100000
# 3 clustered 120 6 -> Mean reward:  0.787160883771711   100000 speed GU 27.7 m/s


# 2 uniform 100 ->     Mean reward:  0.5880605701942397
# 2 clustered 100 2 -> Mean reward:  0.6365822017997916 100000
# 2 clustered 100 4 -> Mean reward:  0.6635439295036311 100000

"""

# For visible check
    env = gym.make('gym_cruising:Cruising-v0', render_mode='human', track_id=2)

    env.action_space.seed(42)

    # ACTOR POLICY NET policy
    transformer_policy = TransformerEncoderDecoder(embed_dim=EMBEDDED_DIM).to(device)
    mlp_policy = MLPPolicyNet(token_dim=EMBEDDED_DIM).to(device)

    PATH_TRANSFORMER = './neural_network/last1Transformer.pth'
    transformer_policy.load_state_dict(torch.load(PATH_TRANSFORMER))
    PATH_MLP_POLICY = './neural_network/last1MLP.pth'
    mlp_policy.load_state_dict(torch.load(PATH_MLP_POLICY))

    options = ({
        "uav": 3,
        "gu": 120,
        "clustered": 1,
        "clusters_number": 3,
        "variance": 100000
    })

    time = int(time.perf_counter())
    print("Time: ", time)
    state, info = env.reset(seed=time, options=options)
    steps = 1
    uav_number = options["uav"]
    while True:
        actions = select_actions(state, uav_number)
        next_state, reward, terminated, truncated, info = env.step(actions)

        if steps == 300:
            truncated = True
        done = terminated or truncated

        state = next_state
        steps += 1

        if done:
            last_RCR = float(info['RCR'])
            break

    env.close()
    print("Last RCR: ", last_RCR)

"""