import os
import torch.optim as optim
import torch
import torch.nn.functional as F
import torch.nn as nn
from torch.distributions import Normal
from torch.optim import Adam
import numpy as np
import random
import copy
import gym
from sklearn.linear_model import LinearRegression
import json
from sklearn.decomposition import PCA
from sklearn.ensemble import GradientBoostingRegressor
# torch.autograd.set_detect_anomaly(True)
import time
import math

if torch.cuda.is_available():
    torch.set_default_tensor_type(torch.cuda.FloatTensor)

class Base_Agent:
    """ A base agent for RL learning. Agent works on the community level to get
    actions for each home energy system (HVAC, Water Heater, PV). """
    def __init__(self, env):
        self.env = env
        self.action_tracker = self.reset_action_tracker()

    def reset_action_tracker(self):
        self.action_tracker = []

    def get_house_action(self, states, uid):
        """ Gets the actions on the house-level """
        pass

    def select_action(self, states):
        """ Analogous to model.predict() in stable baselines.
        Order of action list = [HVAC, Water Heater, PV]
        Returns a dictionary {building unique id : [list of actions]}"""
        pass

class Do_Nothing_Agent(Base_Agent):
    def __init__(self, env):
        super().__init__(env)

    def get_house_action(self, states, uid):
        return [0.0 for _ in range(self.env.action_spaces[uid].shape[0])]

    def select_action(self, states):
        action_dict = {}
        for uid in self.env.buildings.keys():
            action_dict[uid] = self.get_house_action(states, uid)

        return action_dict

class Randomized_Agent(Base_Agent):
    def __init__(self, env):
        super().__init__(env)

    def get_house_action(self, states, uid):
        return self.env.action_spaces[uid].sample()

    def select_action(self, states):
        action_dict = {}
        for uid in self.env.buildings.keys():
            action_dict[uid] = self.get_house_action(states, uid)

        return action_dict


class RBC_Agent:
    def __init__(self, env):
        self.env = env
        self.reset_action_tracker()

    def reset_action_tracker(self):
        self.action_tracker = []

    def select_action(self, states):
        hour_day = states[0]
        daytime = True if hour_day >= 1 and hour_day <= 21 else False
        action_dict = {}
        for uid, states_actions in self.env.buildings_states_actions.items():
            a = []
            for action, enabled in states_actions['actions'].items():
                if enabled:
                    if action == 'cooling_storage' or action == 'dhw_storage':
                        if daytime:
                            a += [-.08]
                        else:
                            a += [0.91]
                    else:
                        a += [0.5]

            action_dict[uid] = a

        return action_dict

class PolicyNetwork(nn.Module):
    def __init__(self, num_inputs, num_actions, action_space, action_scaling_coef, hidden_dim=[400,300],
                 init_w=3e-3, log_std_min=-20, log_std_max=2, epsilon = 1e-6):
        super(PolicyNetwork, self).__init__()

        self.log_std_min = log_std_min
        self.log_std_max = log_std_max
        self.epsilon = epsilon

        self.linear1 = nn.Linear(num_inputs, hidden_dim[0])
        self.linear2 = nn.Linear(hidden_dim[0], hidden_dim[1])

        self.mean_linear = nn.Linear(hidden_dim[1], num_actions)
        self.log_std_linear = nn.Linear(hidden_dim[1], num_actions)

        self.mean_linear.weight.data.uniform_(-init_w, init_w)
        self.mean_linear.bias.data.uniform_(-init_w, init_w)

        self.log_std_linear.weight.data.uniform_(-init_w, init_w)
        self.log_std_linear.bias.data.uniform_(-init_w, init_w)

        self.action_scale = torch.FloatTensor(
            action_scaling_coef * (action_space.high - action_space.low) / 2.)
        self.action_bias = torch.FloatTensor(
            action_scaling_coef * (action_space.high + action_space.low) / 2.)

    def forward(self, state):
        x = F.relu(self.linear1(state))
        x = F.relu(self.linear2(x))
        mean = self.mean_linear(x)
        log_std = self.log_std_linear(x)
        log_std = torch.clamp(log_std, min=self.log_std_min, max=self.log_std_max)
        return mean, log_std

    def sample(self, state):
        mean, log_std = self.forward(state)
        std = log_std.exp()
        normal = Normal(mean, std)
        x_t = normal.rsample()  # for reparameterization trick (mean + std * N(0,1))
        y_t = torch.tanh(x_t)
        action = y_t * self.action_scale + self.action_bias
        log_prob = normal.log_prob(x_t)
        # Enforcing Action Bound
        log_prob -= torch.log(self.action_scale * (1 - y_t.pow(2)) + self.epsilon)
        log_prob = log_prob.sum(1, keepdim=True)
        mean = torch.tanh(mean) * self.action_scale + self.action_bias
        return action, log_prob, mean

    def to(self, device):
        self.action_scale = self.action_scale.to(device)
        self.action_bias = self.action_bias.to(device)
        return super(PolicyNetwork, self).to(device)

class ReplayBuffer:
    def __init__(self, capacity):
        self.capacity = capacity
        self.buffer = []
        self.position = 0

    def push(self, state, action, reward, next_state, done):
        if len(self.buffer) < self.capacity:
            self.buffer.append(None)

        self.buffer[self.position] = (state, action, reward, next_state, done)
        self.position = (self.position + 1) % self.capacity

    def sample(self, batch_size):
        batch = random.sample(self.buffer, batch_size)
        state, action, reward, next_state, done = map(np.stack, zip(*batch))
        return state, action, reward, next_state, done

    def __len__(self):
        return len(self.buffer)

class RegressionBuffer:
    def __init__(self, capacity):
        self.capacity = capacity
        self.x = []
        self.y = []
        self.position = 0

    def push(self, variables, targets):
        if len(self.x) < self.capacity and len(self.x)==len(self.y):
            self.x.append(None)
            self.y.append(None)

        self.x[self.position] = variables
        self.y[self.position] = targets
        self.position = (self.position + 1) % self.capacity

    def __len__(self):
        return len(self.x)

class SoftQNetwork(nn.Module):
    def __init__(self, num_inputs, num_actions, hidden_size=[400,300], init_w=3e-3):
        super(SoftQNetwork, self).__init__()

        self.linear1 = nn.Linear(num_inputs + num_actions, hidden_size[0])
        self.linear2 = nn.Linear(hidden_size[0], hidden_size[1])
        self.linear3 = nn.Linear(hidden_size[1], 1)
        self.ln1 = nn.LayerNorm(hidden_size[0])
        self.ln2 = nn.LayerNorm(hidden_size[1])

        self.linear3.weight.data.uniform_(-init_w, init_w)
        self.linear3.bias.data.uniform_(-init_w, init_w)

    def forward(self, state, action):
        x = torch.cat([state, action], 1)
        x = self.ln1(F.relu(self.linear1(x)))
        x = self.ln2(F.relu(self.linear2(x)))
        x = self.linear3(x)
        return x


class no_normalization:
    def __init__(self):
        pass
    def __mul__(self, x):
        return x

    def __rmul__(self, x):
        return x

class periodic_normalization:
    def __init__(self, x_max):
        self.x_max = x_max
    def __mul__(self, x):
        x = 2 * np.pi * x / self.x_max
        x_sin = np.sin(x)
        x_cos = np.cos(x)
        return np.array([(x_sin+1)/2.0, (x_cos+1)/2.0])
    def __rmul__(self, x):
        x = 2 * np.pi * x / self.x_max
        x_sin = np.sin(x)
        x_cos = np.cos(x)
        return np.array([(x_sin+1)/2.0, (x_cos+1)/2.0])

class onehot_encoding:
    def __init__(self, classes):
        self.classes = classes
    def __mul__(self, x):
        identity_mat = np.eye(len(self.classes))
        return identity_mat[np.array(self.classes) == x][0]
    def __rmul__(self, x):
        identity_mat = np.eye(len(self.classes))
        return identity_mat[np.array(self.classes) == x][0]

class normalize:
    def __init__(self, x_min, x_max):
        self.x_min = x_min
        self.x_max = x_max
    def __mul__(self, x):
        if self.x_min == self.x_max:
            return 0
        else:
            return (x - self.x_min)/(self.x_max - self.x_min)
    def __rmul__(self, x):
        if self.x_min == self.x_max:
            return 0
        else:
            return (x - self.x_min)/(self.x_max - self.x_min)

class remove_feature:
    def __init__(self):
        pass
    def __mul__(self, x):
        return None
    def __rmul__(self, x):
        return None


class RL_Agents_Coord:
    def __init__(self, env, building_uids, hidden_dim=[400,300], discount=0.99, tau=5e-3, lr=3e-4, batch_size=100, replay_buffer_capacity = 1e5, regression_buffer_capacity = 3e4, start_training = None, exploration_period = None, start_regression = None, information_sharing = False, pca_compression = 1., action_scaling_coef = 1., reward_scaling = 1., update_per_step = 1, iterations_as = 2, safe_exploration = False, seed = 0):

        assert start_training > start_regression, 'start_training must be greater than start_regression'

        # with open(buildings_states_actions) as json_file:
        #     self.buildings_states_actions = json.load(json_file)

        self.env = env
        self.buildings_states_actions = env.buildings_states_actions
        self.building_ids = building_uids
        self.building_info = env.get_building_information()
        ospace, aspace = env.get_state_action_spaces()
        self.observation_spaces = dict(zip(env.buildings.keys(), ospace))
        self.action_spaces = env.action_spaces #dict(zip(env.buildings.keys(), aspace))

        self.start_training = start_training
        self.start_regression = start_regression
        self.discount = discount
        self.batch_size = batch_size
        self.tau = tau
        self.action_scaling_coef = action_scaling_coef
        self.reward_scaling = reward_scaling
        self.regression_freq = 2500
        torch.manual_seed(seed)
        np.random.seed(seed)
        self.deterministic = False
        self.information_sharing = information_sharing
        self.update_per_step = update_per_step
        self.iterations_as = iterations_as
        self.safe_exploration = safe_exploration
        self.exploration_period = exploration_period

        self.action_list_ = []
        self.action_list2_ = []

        self.time_step = 0
        self.pca_flag = {uid : 0 for uid in env.buildings.keys()}
        self.regression_flag = {uid : 0 for uid in env.buildings.keys()}
        # self.action_spaces = {uid : a_space for uid, a_space in zip(building_ids, action_spaces)}
        # self.observation_spaces = {uid : o_space for uid, o_space in zip(building_ids, observation_spaces)}

        # Optimizers/Loss using the Huber loss
        self.soft_q_criterion = nn.SmoothL1Loss()

        # device
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.critic1_loss_, self.critic2_loss_, self.actor_loss_, self.alpha_loss_, self.alpha_, self.q_tracker = {}, {}, {}, {}, {}, {}

        self.energy_size_coef = {}
        self.total_coef = 0
        for uid, info in self.building_info.items():
            _coef = info['Annual_DHW_demand (kWh)']/.9 + info['Annual_cooling_demand (kWh)']/3.5 + info['Annual_nonshiftable_electrical_demand (kWh)'] - info['solar_power_capacity (kW)']*8760/6.0
            self.energy_size_coef[uid] = max(.3*(_coef + info['solar_power_capacity (kW)']*8760/6.0), _coef)/8760
            self.total_coef += self.energy_size_coef[uid]

        for uid in self.energy_size_coef:
            self.energy_size_coef[uid] = self.energy_size_coef[uid]/self.total_coef

        self.replay_buffer, self.reg_buffer, self.soft_q_net1, self.soft_q_net2, self.target_soft_q_net1, self.target_soft_q_net2, self.policy_net, self.soft_q_optimizer1, self.soft_q_optimizer2, self.policy_optimizer, self.target_entropy, self.alpha, self.log_alpha, self.alpha_optimizer, self.pca, self.encoder, self.encoder_reg, self.state_estimator, self.norm_mean, self.norm_std, self.r_norm_mean, self.r_norm_std, self.log_pi_tracker = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}
        for uid in env.buildings.keys():
            self.state_estimator[uid] = GradientBoostingRegressor()
            self.critic1_loss_[uid], self.critic2_loss_[uid], self.actor_loss_[uid], self.alpha_loss_[uid], self.alpha_[uid], self.q_tracker[uid], self.log_pi_tracker[uid] = [], [], [], [], [], [], []
            self.encoder[uid] = []
            state_n = 0
            for s_name, s in self.buildings_states_actions[uid]['states'].items():
                if not s:
                    self.encoder[uid].append(0)
                elif s_name in ["month", "hour"]:
                    self.encoder[uid].append(periodic_normalization(self.observation_spaces[uid].high[state_n]))
                    state_n += 1
                elif s_name == "day":
                    self.encoder[uid].append(onehot_encoding([1,2,3,4,5,6,7,8]))
                    state_n += 1
                elif s_name == "daylight_savings_status":
                    self.encoder[uid].append(onehot_encoding([0,1]))
                    state_n += 1
                elif s_name == "net_electricity_consumption":
                    self.encoder[uid].append(remove_feature())
                    state_n += 1
                else:
                    self.encoder[uid].append(normalize(self.observation_spaces[uid].low[state_n], self.observation_spaces[uid].high[state_n]))
                    state_n += 1

            self.encoder[uid] = np.array(self.encoder[uid])

            # If there is no solar PV installed, remove solar radiation variables
            if self.building_info[uid]['solar_power_capacity (kW)'] == 0:
                for k in range(12,20):
                    if self.encoder[uid][k] != 0:
                        self.encoder[uid][k] = -1
                if self.encoder[uid][24] != 0:
                    self.encoder[uid][24] = -1
            if self.building_info[uid]['Annual_DHW_demand (kWh)'] == 0 and self.encoder[uid][26] != 0:
                self.encoder[uid][26] = -1
            if self.building_info[uid]['Annual_cooling_demand (kWh)'] == 0 and self.encoder[uid][25] != 0:
                self.encoder[uid][25] = -1
            if self.building_info[uid]['Annual_nonshiftable_electrical_demand (kWh)'] == 0 and self.encoder[uid][23] != 0:
                self.encoder[uid][23] = -1

            self.encoder[uid] = self.encoder[uid][self.encoder[uid]!=0]
            self.encoder[uid][self.encoder[uid]==-1] = remove_feature()


            # Defining the encoder that will transform the states used by the regression model to predict the net-electricity consumption
            self.encoder_reg[uid] = []
            state_n = 0
            for s_name, s in self.buildings_states_actions[uid]['states'].items():
                if not s:
                    self.encoder_reg[uid].append(0)
                elif s_name in ["month", "hour"]:
                    self.encoder_reg[uid].append(periodic_normalization(self.observation_spaces[uid].high[state_n]))
                    state_n += 1
                elif s_name in ["t_out_pred_6h","t_out_pred_12h","t_out_pred_24h","rh_out_pred_6h","rh_out_pred_12h","rh_out_pred_24h","diffuse_solar_rad_pred_6h","diffuse_solar_rad_pred_12h","diffuse_solar_rad_pred_24h","direct_solar_rad_pred_6h","direct_solar_rad_pred_12h","direct_solar_rad_pred_24h"]:
                    self.encoder_reg[uid].append(remove_feature())
                    state_n += 1
                else:
                    self.encoder_reg[uid].append(no_normalization())
                    state_n += 1

            self.encoder_reg[uid] = np.array(self.encoder_reg[uid])

            # If there is no solar PV installed, remove solar radiation variables
            if self.building_info[uid]['solar_power_capacity (kW)'] == 0:
                for k in range(12,20):
                    if self.encoder_reg[uid][k] != 0:
                        self.encoder_reg[uid][k] = -1
                if self.encoder_reg[uid][24] != 0:
                    self.encoder_reg[uid][24] = -1
            if self.building_info[uid]['Annual_DHW_demand (kWh)'] == 0 and self.encoder_reg[uid][26] != 0:
                self.encoder_reg[uid][26] = -1
            if self.building_info[uid]['Annual_cooling_demand (kWh)'] == 0 and self.encoder_reg[uid][25] != 0:
                self.encoder_reg[uid][25] = -1
            if self.building_info[uid]['Annual_nonshiftable_electrical_demand (kWh)'] == 0 and self.encoder_reg[uid][23] != 0:
                self.encoder_reg[uid][23] = -1

            self.encoder_reg[uid] = self.encoder_reg[uid][self.encoder_reg[uid]!=0]
            self.encoder_reg[uid][self.encoder_reg[uid]==-1] = remove_feature()


            # PCA will reduce the number of dimensions of the state space to 2/3 of its the original size
            len_obs = len([j for j in np.hstack(self.encoder[uid]*np.ones(len(self.observation_spaces[uid].low))) if j != None])
            if self.information_sharing:
                state_dim = int(pca_compression*(2 + len_obs))
            else:
                state_dim = int(pca_compression*len_obs)

            action_dim = self.action_spaces[uid].shape[0]
            self.alpha[uid] = 0.2

            self.pca[uid] = PCA(n_components = state_dim)

            self.replay_buffer[uid] = ReplayBuffer(int(replay_buffer_capacity))
            self.reg_buffer[uid] = RegressionBuffer(int(regression_buffer_capacity))

            # init networks
            self.soft_q_net1[uid] = SoftQNetwork(state_dim, action_dim, hidden_dim).to(self.device)
            self.soft_q_net2[uid] = SoftQNetwork(state_dim, action_dim, hidden_dim).to(self.device)

            self.target_soft_q_net1[uid] = SoftQNetwork(state_dim, action_dim, hidden_dim).to(self.device)
            self.target_soft_q_net2[uid] = SoftQNetwork(state_dim, action_dim, hidden_dim).to(self.device)

            for target_param, param in zip(self.target_soft_q_net1[uid].parameters(), self.soft_q_net1[uid].parameters()):
                target_param.data.copy_(param.data)

            for target_param, param in zip(self.target_soft_q_net2[uid].parameters(), self.soft_q_net2[uid].parameters()):
                target_param.data.copy_(param.data)


            # Policy
            self.policy_net[uid] = PolicyNetwork(state_dim, action_dim, self.action_spaces[uid], self.action_scaling_coef, hidden_dim).to(self.device)
            self.soft_q_optimizer1[uid] = optim.Adam(self.soft_q_net1[uid].parameters(), lr=lr)
            self.soft_q_optimizer2[uid] = optim.Adam(self.soft_q_net2[uid].parameters(), lr=lr)
            self.policy_optimizer[uid] = optim.Adam(self.policy_net[uid].parameters(), lr=lr)
            self.target_entropy[uid] = -np.prod(self.action_spaces[uid].shape).item()
            self.log_alpha[uid] = torch.zeros(1, requires_grad=True, device=self.device)
            self.alpha_optimizer[uid] = optim.Adam([self.log_alpha[uid]], lr=lr)

    def select_action(self, states, _building_ids=None, deterministic=False):

        self.time_step += 1
        # print(self.time_step)
        explore = self.time_step <= self.exploration_period

        n_iterations = self.iterations_as

        if not _building_ids:
            action_order = np.array(range(len(self.building_ids)))
            np.random.shuffle(action_order)

            _building_ids = [self.building_ids[i] for i in action_order]
            _building_ids_next = [self.building_ids[action_order[(i+1)%len(action_order)]] for i in range(len(action_order))]
            _states = [states[i] for i in action_order]

        actions = [None for _ in range(len(_building_ids))]

        # Initialize coordination vars. Accumulated net electricity consumption = 0, queue = 0
        accum_net_electric_demand = 0.
        coordination_variables = [[None, None] for _ in range(len(_building_ids))]

        coordination_vars = {key:np.array([0., 0.]) for key in _building_ids}
        expected_demand = {key:0. for key in _building_ids}

        _total_demand = 0
        capacity_dispatched = 0
        k = 0
        if explore:
            for uid, uid_next, state in zip(_building_ids, _building_ids_next, _states):
                if self.safe_exploration:
                    hour_day = state[2]
                    a_dim = len(self.action_spaces[uid].sample())

                    # Daytime: release stored energy
                    act = [0.0 for _ in range(a_dim)]
                    if hour_day >= 9 and hour_day <= 21:
                        act = [-0.08 for _ in range(a_dim)]

                    # Early nightime: store DHW and/or cooling energy
                    if (hour_day >= 1 and hour_day <= 8) or (hour_day >= 22 and hour_day <= 24):
                        act = [0.091 for _ in range(a_dim)]

                else:
                    act = self.action_scaling_coef*self.action_spaces[uid].sample()

                actions[action_order[k]] = act
                k += 1

                if self.time_step > self.start_regression and self.information_sharing:
                    x_reg = np.hstack(np.concatenate(([j for j in np.hstack(self.encoder_reg[uid]*state) if j != None][:-1], act)))
                    expected_demand[uid] = self.state_estimator[uid].predict(x_reg.reshape(1,-1))
                    _total_demand += expected_demand[uid]

                    coordination_vars[uid][1] = capacity_dispatched
                    capacity_dispatched += self.energy_size_coef[uid]

            if self.time_step > self.start_regression and self.information_sharing:
                for uid, uid_next in zip(_building_ids, _building_ids_next):
                    coordination_vars[uid][0] = (_total_demand - expected_demand[uid])/self.total_coef

                k = 0
                for uid in _building_ids:
                    coordination_variables[action_order[k]][0] = coordination_vars[uid][0]
                    coordination_variables[action_order[k]][1] = coordination_vars[uid][1]
                    k += 1
        else:
            if self.information_sharing:
                n = 0
                while n < n_iterations:
                    capacity_dispatched = 0
                    for uid, uid_next, state in zip(_building_ids, _building_ids_next, _states):
                        foo = np.hstack(self.encoder[uid]*state)
                        state_ = np.array([j for j in foo if j != None])

                        # Adding shared information to the state
                        if self.information_sharing:
                            state_ = np.hstack(np.concatenate((state_, coordination_vars[uid])))

                        state_ = (state_  - self.norm_mean[uid])/self.norm_std[uid]
                        state_ = self.pca[uid].transform(state_.reshape(1,-1))[0]
                        state_ = torch.FloatTensor(state_).unsqueeze(0).to(self.device)

                        if deterministic is False:
                            act, _, _ = self.policy_net[uid].sample(state_)
                        else:
                            _, _, act = self.policy_net[uid].sample(state_)

                        # Get the actions in the last iterations if sharing information
                        if n == n_iterations-1:
                            actions[action_order[k]] = act.detach().cpu().numpy()[0]
                            k += 1

                        x_reg = np.hstack(np.concatenate(([j for j in np.hstack(self.encoder_reg[uid]*state) if j != None][:-1], act.detach().squeeze(0).cpu().numpy())))
                        expected_demand[uid] = self.state_estimator[uid].predict(x_reg.reshape(1,-1))

                        if n == n_iterations-1 and uid == _building_ids[-1]:
                            pass
                            # _total_demand += expected_demand[uid]
                        else:
                            _total_demand += expected_demand[uid] - expected_demand[uid_next]

                        coordination_vars[uid][1] = capacity_dispatched
                        capacity_dispatched += self.energy_size_coef[uid]

                        if n == n_iterations-1 and uid == _building_ids[-1]:
                            pass
                        else:
                            coordination_vars[uid_next][0] = _total_demand/self.total_coef
                    n += 1

                k = 0
                for uid in _building_ids:
                    coordination_variables[action_order[k]][0] = coordination_vars[uid][0]
                    coordination_variables[action_order[k]][1] = coordination_vars[uid][1]
                    k += 1
            else:
                for uid, uid_next, state in zip(_building_ids, _building_ids_next, _states):
                    state_ = np.array([j for j in np.hstack(self.encoder[uid]*state) if j != None])

                    state_ = (state_  - self.norm_mean[uid])/self.norm_std[uid]
                    state_ = self.pca[uid].transform(state_.reshape(1,-1))[0]
                    state_ = torch.FloatTensor(state_).unsqueeze(0).to(self.device)

                    if deterministic is False:
                        act, _, _ = self.policy_net[uid].sample(state_)
                    else:
                        _, _, act = self.policy_net[uid].sample(state_)

                    actions[action_order[k]] = act.detach().cpu().numpy()[0]
                    k += 1


        action_dict = {i:[] for i in _building_ids}
        for i in range(len(_building_ids)):
            action_dict[_building_ids[i]] = actions[action_order[i]]

        ordered_action_dict = {i:action_dict[i] for i in self.building_ids}
        return ordered_action_dict, np.array(coordination_variables)


    def add_to_buffer(self, states, actions, rewards, next_states, done, coordination_vars, coordination_vars_next):
        zipped = zip(self.building_ids, states, list(actions.values()), rewards, next_states, coordination_vars, coordination_vars_next)

        for (uid, o, a, r, o2, coord_vars, coord_vars_next) in zipped:
            if self.information_sharing:
                # Normalize all the states using periodical normalization, one-hot encoding, or -1, 1 scaling. It also removes states that are not necessary (solar radiation if there are no solar PV panels).
                x_reg = np.hstack(np.concatenate(([j for j in np.hstack(self.encoder_reg[uid]*o) if j != None][:-1], a)))
                y_reg = [j for j in np.hstack(self.encoder_reg[uid]*o2) if j != None][-1]

                # Push inputs and targets to the regression buffer. The targets are the net electricity consumption.
                self.reg_buffer[uid].push(x_reg, y_reg)

            # Run once the regression model has been fitted
            if self.regression_flag[uid] > 1:
                # Normalize all the states using periodical normalization, one-hot encoding, or -1, 1 scaling. It also removes states that are not necessary (solar radiation if there are no solar PV panels).
                o = np.array([j for j in np.hstack(self.encoder[uid]*o) if j != None])
                o2 = np.array([j for j in np.hstack(self.encoder[uid]*o2) if j != None])

                # Only executed during the random exploration phase. Pushes unnormalized tuples into the replay buffer.

                if self.information_sharing:
                    o = np.hstack(np.concatenate((o, coord_vars)))
                    o2 = np.hstack(np.concatenate((o2, coord_vars_next)))

                # Executed during the training phase. States and rewards pushed into the replay buffer are normalized and processed using PCA.
                if self.pca_flag[uid] == 1:
                    o = (o - self.norm_mean[uid])/self.norm_std[uid]
                    o = self.pca[uid].transform(o.reshape(1,-1))[0]
                    o2 = (o2 - self.norm_mean[uid])/self.norm_std[uid]
                    o2 = self.pca[uid].transform(o2.reshape(1,-1))[0]
                    r = (r - self.r_norm_mean[uid])/self.r_norm_std[uid]

                self.replay_buffer[uid].push(o, a, r, o2, done)

            if self.time_step >= self.start_regression and (self.regression_flag[uid] < 2 or self.time_step % self.regression_freq == 0):
                if self.information_sharing:
                    # Fit regression model for the first time.
                    self.state_estimator[uid].fit(self.reg_buffer[uid].x, self.reg_buffer[uid].y)

                if self.regression_flag[uid] < 2:
                    self.regression_flag[uid] += 1

        if self.time_step >= self.start_training and self.batch_size <= len(self.replay_buffer[self.building_ids[0]]):
            for uid in self.building_ids:
                # This code only runs once. Once the random exploration phase is over, we normalize all the states and rewards to make them have mean=0 and std=1, and apply PCA. We push the normalized compressed values back into the buffer, replacing the old buffer.
                if self.pca_flag[uid] == 0:
                    # print("normalizing...")
                    X = np.array([j[0] for j in self.replay_buffer[uid].buffer])
                    self.norm_mean[uid] = np.mean(X, axis=0)
                    self.norm_std[uid] = np.std(X, axis=0) + 1e-5
                    X = (X - self.norm_mean[uid])/self.norm_std[uid]

                    R = np.array([j[2] for j in self.replay_buffer[uid].buffer])
                    self.r_norm_mean[uid] = np.mean(R)
                    self.r_norm_std[uid] = np.std(R)/self.reward_scaling + 1e-5

                    # print(X.shape)
                    self.pca[uid].fit(X)
                    new_buffer = []
                    for s, a, r, s2, dones in self.replay_buffer[uid].buffer:
                        s_buffer = np.hstack(self.pca[uid].transform(((s - self.norm_mean[uid])/self.norm_std[uid]).reshape(1,-1))[0])
                        s2_buffer = np.hstack(self.pca[uid].transform(((s2 - self.norm_mean[uid])/self.norm_std[uid]).reshape(1,-1))[0])
                        new_buffer.append((s_buffer, a, (r - self.r_norm_mean[uid])/self.r_norm_std[uid], s2_buffer, dones))

                    self.replay_buffer[uid].buffer = new_buffer
                    self.pca_flag[uid] = 1

            # for _ in range(1 + max(0, self.time_step - 8760)//5000):
            for _ in range(self.update_per_step):
                for uid in self.building_ids:
                    state, action, reward, next_state, done = self.replay_buffer[uid].sample(self.batch_size)

                    if self.device.type == "cuda":
                        state      = torch.cuda.FloatTensor(state).to(self.device)
                        next_state = torch.cuda.FloatTensor(next_state).to(self.device)
                        action     = torch.cuda.FloatTensor(action).to(self.device)
                        reward     = torch.cuda.FloatTensor(reward).unsqueeze(1).to(self.device)
                        done       = torch.cuda.FloatTensor(done).unsqueeze(1).to(self.device)
                    else:
                        state      = torch.FloatTensor(state).to(self.device)
                        next_state = torch.FloatTensor(next_state).to(self.device)
                        action     = torch.FloatTensor(action).to(self.device)
                        reward     = torch.FloatTensor(reward).unsqueeze(1).to(self.device)
                        done       = torch.FloatTensor(done).unsqueeze(1).to(self.device)

                    with torch.no_grad():
                        # Update Q-values. First, sample an action from the Gaussian policy/distribution for the current (next) state and its associated log probability of occurrence.
                        new_next_actions, new_log_pi, _ = self.policy_net[uid].sample(next_state)

                        # The updated Q-value is found by subtracting the logprob of the sampled action (proportional to the entropy) to the Q-values estimated by the target networks.
                        target_q_values = torch.min(
                                        self.target_soft_q_net1[uid](next_state, new_next_actions),
                                        self.target_soft_q_net2[uid](next_state, new_next_actions),
                                    ) - self.alpha[uid] * new_log_pi

                        q_target = reward + (1 - done) * self.discount * target_q_values
                        self.q_tracker[uid].append(q_target.mean())

                    # Update Soft Q-Networks
                    q1_pred = self.soft_q_net1[uid](state, action)
                    q2_pred = self.soft_q_net2[uid](state, action)

                    q1_loss = self.soft_q_criterion(q1_pred, q_target)
                    q2_loss = self.soft_q_criterion(q2_pred, q_target)


                    self.soft_q_optimizer1[uid].zero_grad()
                    q1_loss.backward()
                    self.soft_q_optimizer1[uid].step()

                    self.soft_q_optimizer2[uid].zero_grad()
                    q2_loss.backward()
                    self.soft_q_optimizer2[uid].step()

                    # Update Policy
                    new_actions, log_pi, _ = self.policy_net[uid].sample(state)

                    q_new_actions = torch.min(
                        self.soft_q_net1[uid](state, new_actions),
                        self.soft_q_net2[uid](state, new_actions)
                    )

                    policy_loss = (self.alpha[uid]*log_pi - q_new_actions).mean()

                    self.policy_optimizer[uid].zero_grad()
                    policy_loss.backward()
                    self.policy_optimizer[uid].step()


                    # Optimize the temperature parameter alpha, used for exploration through entropy maximization
                    # alpha_loss = -(self.log_alpha[uid] * (log_pi + self.target_entropy[uid]).detach()).mean()
                    # self.alpha_optimizer[uid].zero_grad()
                    # alpha_loss.backward()
                    # self.alpha_optimizer[uid].step()
                    self.alpha[uid] = 0.2 #self.log_alpha[uid].exp()

                    # self.critic1_loss_[uid].append(q1_loss.item())
                    # self.critic2_loss_[uid].append(q2_loss.item())
                    # self.actor_loss_[uid].append(policy_loss.item())
                    # self.alpha_loss_[uid].append(alpha_loss.item())
                    # self.alpha_[uid].append(self.alpha[uid].item())
                    # self.log_pi_tracker[uid].append(log_pi.mean())

                    # Soft Updates
                    for target_param, param in zip(self.target_soft_q_net1[uid].parameters(), self.soft_q_net1[uid].parameters()):
                        target_param.data.copy_(
                            target_param.data * (1.0 - self.tau) + param.data * self.tau
                        )

                    for target_param, param in zip(self.target_soft_q_net2[uid].parameters(), self.soft_q_net2[uid].parameters()):
                        target_param.data.copy_(
                            target_param.data * (1.0 - self.tau) + param.data * self.tau
                        )

class Cluster_Agents:
    def __init__(self, env, n_clusters=6, hidden_dim=[400,300], discount=0.99, tau=5e-3, lr=3e-4, batch_size=100, replay_buffer_capacity=1e5, regression_buffer_capacity=3e4, start_training=None, exploration_period=None, start_regression=None, information_sharing=False, pca_compression=1., action_scaling_coef=1., reward_scaling=1., update_per_step=1, iterations_as=2, safe_exploration=False, seed=0):
        self.env = env
        self.hidden_dim = hidden_dim
        self.discount = discount
        self.tau = tau
        self.lr = lr
        self.batch_size = batch_size
        self.replay_buffer_capacity = replay_buffer_capacity
        self.regression_buffer_capacity = regression_buffer_capacity
        self.start_training = start_training
        self.exploration_period = exploration_period
        self.start_regression = start_regression
        self.information_sharing = information_sharing
        self.pca_compression = pca_compression
        self.action_scaling_coef = action_scaling_coef
        self.reward_scaling = reward_scaling
        self.update_per_step = update_per_step
        self.iterations_as = iterations_as
        self.safe_exploration = safe_exploration
        self.seed = seed
        self.agents = self.create_clusters(n_clusters)

    def create_clusters(self, n_clusters):
        agents = []

        # group clusters by spatial diversity
        cluster = 0
        building_uids = [[] for _ in range(n_clusters)]
        for i in range(len(self.env.net.load)):
            building_uids[cluster] += [self.env.net.load.name.iloc[i]]
            cluster = (cluster + 1) % n_clusters

        for i in range(n_clusters):
            agents += [RL_Agents_Coord(self.env, building_uids[i], hidden_dim=self.hidden_dim, discount=self.discount, tau=self.tau, lr=self.lr, batch_size=self.batch_size, replay_buffer_capacity=self.replay_buffer_capacity, regression_buffer_capacity=self.regression_buffer_capacity, start_training=self.start_training, exploration_period=self.exploration_period, start_regression=self.start_regression, information_sharing=self.information_sharing, pca_compression=self.pca_compression, action_scaling_coef=self.action_scaling_coef, reward_scaling=self.reward_scaling, update_per_step=self.update_per_step, iterations_as=self.iterations_as, safe_exploration=self.safe_exploration, seed=self.seed)]

        return agents

    def step(self, state, is_evaluating):
        for agent in self.agents:
            next_state, reward, done, _ = self.env.step(agent.action)
            next_action, coordination_vars_next = agent.select_action(next_state, deterministic=is_evaluating)
            agent.add_to_buffer(state, agent.action, reward, next_state, done, agent.coordination_vars, coordination_vars_next)

            state = next_state
            agent.coordination_vars = coordination_vars_next
            agent.action = next_action

        return state,done
