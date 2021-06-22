from torch import optim
from avalanche.training.strategies.base_strategy import BaseStrategy
import torch
import torch.nn as nn
from torch.optim.optimizer import Optimizer
import gym
from gym import Env
from avalanche.benchmarks.rl_benchmark import RLExperience, RLScenario
from typing import Union, Optional, Sequence, List, Dict, Tuple
from dataclasses import dataclass
import numpy as np
from avalanche.training.plugins.strategy_plugin import StrategyPlugin
from avalanche.training.strategies.rl_utils import *
from collections import defaultdict
import random
import copy
import enum


@dataclass
class Step:
    """ Holds vectorized environment steps result of size `n_envs` x D. """
    states: Union[np.ndarray, torch.Tensor]
    actions: Union[np.ndarray, torch.Tensor]
    dones: Union[bool, np.ndarray]
    rewards: Union[float, np.ndarray]
    next_states: Union[np.ndarray, torch.Tensor]

    def __post_init__(self):
        # make sure no graph's ever attached by mistake
        for k in self.__annotations__:
            var = getattr(self, k)
            if type(var) is torch.Tensor:
                setattr(self, k, var.detach())
    @property
    def n_envs(self):
        # FIXME: return self.states.shape[0] 
        return 1


@dataclass
class Rollout:
    steps: List[Step]

    @property
    def n_envs(self):
        return self.steps[0].n_envs

    @property
    def rewards(self):
        """
            Returns all rewards gathered at each step of this rollout.
        """
        # batch dimension over number of parallel environments, instatiate transpose for faster assignment
        rwds = torch.zeros(
            (len(self.steps), self.n_envs),
            dtype=torch.float32)
        for i, step in enumerate(self.steps):
            rwds[i] = step.rewards
        return rwds.T

    @property
    def dones(self):
        """
            Returns terminal state flag of each step of this rollout.
        """
        # batch dimension over number of parallel environments, instatiate transpose for faster assignment
        dones = torch.zeros(
            (len(self.steps), self.n_envs),
            dtype=torch.float32)
        for i, step in enumerate(self.steps):
            dones[i] = step.dones
        return dones.bool().T

    @property
    def actions(self):
        """
            Returns all actions taken at each step of this rollout.
        """
        # batch dimension over number of parallel environments, instatiate transpose for faster assignment
        actions = torch.zeros(
            (len(self.steps), self.n_envs),
            dtype=torch.float32)
        for i, step in enumerate(self.steps):
            actions[i] = step.actions
        return actions.T

    def _get_obs(self, prop_name: str, as_tensor=True):
        """
            Returns all observations gathered at each step of this rollout.
        """
        mlib = torch if as_tensor else np
        obs = mlib.zeros(
            (len(self.steps), *self.steps[0].states.shape),
            dtype=mlib.float32)
        for i, step in enumerate(self.steps):
            obs[i] = getattr(step, prop_name)

        # batch dimension over number of parallel environments
        # FIXME: think about batch dim for images return mlib.swapaxes(obs, 0, 1)
        return obs

    @property
    def observations(self):
        return self._get_obs('states')

    @property
    def next_observations(self):
        return self._get_obs('next_states')

    def squeeze_timestep_dimension(self):
        """
            Reshapes gathered steps from n x t x D to (n+t) x D where n = n_envs 
            and t is the length of the rollout.
        """
        pass


@dataclass
class ReplayMemory:
    # like a Rollout but with time-indipendent Steps 
    _memory: List[Step]
    """ Max number of Steps contained inside memory. When trying to add a new Step and size is reached, a previous Step is replaced. """
    size: int
    steps_counter: int = 0

    @property
    def n_envs(self):
        return self._memory[0].n_envs

    def add_rollouts(self, rollouts: List[Rollout]):
        """
            Adds a list of rollouts to the memory, "merging" n_envs and timestep 
            dimension so that we can sample more efficiently from a simple list 
            of steps (n x t x D -> (n+t) x D) and handle overwriting of new experience.

        Args:
            rollouts (List[Rollout]): [description]
        """
        # increase sample counter counting data from different actors as separate samples
        for rollout in rollouts:
            new_sc = rollout.n_envs * len(rollout.steps)
            for s, i in enumerate(
                                  range(
                                      self.steps_counter, self.steps_counter +
                                      new_sc)):
                if i < self.size:
                    self._memory.append(rollout.steps[s])
                else:
                    self._memory[i % self.size] = rollout.steps[s]

            self.steps_counter += new_sc

    def sample_batch(self, batch_dim: int):
        """Sample a batch of Steps of shape B x D where 

        Args:
            batch_dim (int): [description]

        Returns:
            [type]: [description]
        """
        return np.random.choice(self._memory, size=batch_dim, replace=False)


class TimestepUnit(enum.IntEnum):
    STEPS = 0
    EPISODES = 1


@dataclass
class Timestep:
    value: int
    unit: TimestepUnit = TimestepUnit.STEPS

# TODO: evaluation
class RLBaseStrategy(BaseStrategy):
    def __init__(
                self, model: nn.Module, optimizer: Optimizer, per_experience_steps: Union[int, Timestep], criterion=nn.MSELoss(),
                rollouts_per_step: int = 1, max_steps_per_rollout: int = -1, updates_per_step: int = 1,
                device='cpu',
                plugins: Optional[Sequence[StrategyPlugin]] = [],
                discount_factor: float = 0.99,
                # evaluator=default_logger,
                eval_every=-1):

        super().__init__(model, optimizer, criterion=criterion, device=device,
                         plugins=plugins, eval_every=eval_every)
        # if a single integer is passed, assume it's steps
        if type(per_experience_steps) is int:
            per_experience_steps = Timestep(per_experience_steps)

        self.per_experience_steps: Timestep = per_experience_steps
        self.rollouts_per_step = rollouts_per_step
        self.max_steps_per_rollout = max_steps_per_rollout
        self.updates_per_step = updates_per_step
        self.total_steps = 0
        self._obs: torch.Tensor = None
        # self.plugins = plugins
        # self.device = device
        self.gamma = discount_factor

    # Additional callback added by RLBaseStrategy
    def before_rollout(self, **kwargs):
        for p in self.plugins:
            p.before_rollout(self, **kwargs)

    def after_rollout(self, **kwargs):
        for p in self.plugins:
            p.after_rollout(self, **kwargs)

    def sample_rollout_action(self, observations: torch.Tensor):
        raise NotImplementedError(
            "`sample_rollout_action` must be implemented by every RL strategy")

    def rollout(
            self, env: Env, n_rollouts: int, max_steps: int = -1) -> Tuple[List[Rollout], int]:
        # gather experience from env
        t = 0
        rollouts = []
        for _ in range(n_rollouts):
            step_experiences = []
            # reset environment only after completing episodes
            if self._obs is None:
                self._obs = env.reset()
            done = False
            while not done:
                action = self.sample_rollout_action(self._obs) 
                # TODO: handle automatic reset for parallel envs with different lenght (must concatenate different episodes)
                next_obs, reward, done, _ = env.step(action)
                step_experiences.append(
                    Step(self._obs, action, done, reward, next_obs))
                t += 1
                self._obs = next_obs
                # TODO: quit when one env is done? should be handled by vecenv as default
                if done:
                    self._obs = None
                    break
                if max_steps > 0 and t >= max_steps:
                    break
            rollouts.append(Rollout(step_experiences))
        return rollouts, t

    def update(self, rollouts: List[Rollout], n_update_steps: int, **kwargs):
        raise NotImplementedError(
            "`update` must be implemented by every RL strategy")

    # FIXME: support old callbacks
    def before_training(self, **kwargs):
        for p in self.plugins:
            p.before_training(self, **kwargs)

    def after_training(self, **kwargs):
        for p in self.plugins:
            p.after_training(self, **kwargs)

    def make_env(self, **kwargs):
        # TODO: can be passed as argument to specify transformations
        return Array2Tensor(self.environment)

    def train(self, experiences: Union[RLExperience, Sequence[RLExperience]],
              eval_streams: Optional[Sequence[Union[RLExperience,
                                                    Sequence[
                                                        RLExperience]]]] = None,
              **kwargs):
        self.is_training = True
        self.model.train()
        self.model.to(self.device)

        # Normalize training and eval data.
        if isinstance(experiences, RLExperience):
            experiences = [experiences]
        if eval_streams is None:
            eval_streams = [experiences]
        for i, exp in enumerate(eval_streams):
            if isinstance(exp, RLExperience):
                eval_streams[i] = [exp]

        self.before_training(**kwargs)
        for self.experience in experiences:
            # make sure env is reset on new experience
            self._obs = None
            self.train_exp(self.experience, eval_streams, **kwargs)
        self.after_training(**kwargs)

        self.is_training = False
        # res = self.evaluator.get_last_metrics()
        # return res

    def train_exp(self, experience: RLExperience, eval_streams=None, **kwargs):
        self.environment = experience.environment
        self.rollout_steps = 0

        # Data Adaptation (e.g. add new samples/data augmentation)
        # self.before_train_dataset_adaptation(**kwargs)
        # self.train_dataset_adaptation(**kwargs)
        # self.after_train_dataset_adaptation(**kwargs)
        # self.make_train_dataloader(**kwargs)
        self.environment = self.make_env(**kwargs)

        # Model Adaptation (e.g. freeze/add new units)
        # self.model_adaptation()
        self.make_optimizer()

        self.before_training_exp(**kwargs)

        # either run N episodes or steps depending on specified `per_experience_steps`
        for self.timestep in range(self.per_experience_steps.value):
            self.before_rollout(**kwargs)
            self.rollouts, steps = self.rollout(
                env=self.environment, n_rollouts=self.rollouts_per_step,
                max_steps=self.max_steps_per_rollout)
            self.after_rollout(**kwargs)
            # TODO: to keep track in default evaluator and do that in callback
            self.rollout_steps += steps

            # update must instatiate `self.loss`
            self.update(self.rollouts, self.updates_per_step)

            # Loss & Backward
            self.before_backward(**kwargs)
            self.loss.backward()
            self.after_backward(**kwargs)

            # Optimization step
            self.before_update(**kwargs)
            self.optimizer.step()
            self.after_update(**kwargs)

        self.total_steps += self.rollout_steps


from avalanche.models.actor_critic import ActorCriticMLP
from avalanche.models.dqn import ConvDeepQN
from torch.optim import Optimizer
from torch.distributions import Categorical


class A2CStrategy(RLBaseStrategy):

    def __init__(
            self, model: nn.Module, optimizer: Optimizer,
            per_experience_steps: Union[int, Timestep],
            max_steps_per_rollout: int = 1, value_criterion=nn.MSELoss(),
            discount_factor: float = 0.99, device='cpu',
            updates_per_step: int = 1,
            plugins: Optional[Sequence[StrategyPlugin]] = [],
            eval_every=-1, policy_loss_weight: float = 0.5,
            value_loss_weight: float = 0.5,):
        super().__init__(
            model, optimizer, per_experience_steps=per_experience_steps,
            # here we make sure we can only do steps not episodes
            rollouts_per_step=max_steps_per_rollout,
            max_steps_per_rollout=max_steps_per_rollout,
            updates_per_step=updates_per_step, device=device, plugins=plugins,
            discount_factor=discount_factor, eval_every=eval_every)
        assert self.per_experience_steps.unit == TimestepUnit.STEPS, 'A2C only supports expressing training duration in steps not episodes'

        # TODO: 'dataloader' calls with pre-processing env wrappers 
        self.model = model
        self.optimizer = optimizer
        self.value_criterion = value_criterion
        self.ac_w = policy_loss_weight
        self.cr_w = value_loss_weight

    def sample_rollout_action(self, observations: torch.Tensor):
        """
            This will process a batch of observations and produce a batch
            of actions to better leverage GPU as in 'batched' A2C.
        Args:
            observations (torch.Tensor): [description]

        Returns:
            [type]: [description]
        """
        # sample action from policy network
        # FIXME: Remove and add vecenv
        observations = observations.unsqueeze(0)
        with torch.no_grad():
            # policy_only forward?
            print("Sampling action!", observations.shape, observations.dtype)
            _, policy_logits = self.model(observations, compute_value=False)
        # FIXME: remove item and add vecenv (alternative np.random.choice(num_outputs, p=np.squeeze(dist)))
        return Categorical(logits=policy_logits).sample().item()

    def update(self, rollouts: List[Rollout], n_update_steps: int, **kwargs):
        # perform gradient step(s) over batch of gathered rollouts
        # TODO: rollout buffer to avoid loop
        # TODO: before/after forward
        self.optimizer.zero_grad()
        self.loss = 0
        for rollout in rollouts:
            # print("Rollout Observation shape", rollout.observations.shape)
            values, policy_logits = self.model(rollout.observations)
            # ~log(softmax(action_logits))
            # print("Rollout Actions shape", rollout.actions.shape)
            log_prob = Categorical(
                logits=policy_logits).log_prob(
                rollout.actions)
            # compute next states values
            next_values, _ = self.model(
                rollout.next_observations, compute_policy=False)
            # mask terminal states values
            next_values[rollout.dones] = 0.

            # print("Rollout Rewards shape", rollout.rewards.shape)
            # Actor/Policy Loss Term in A2C: A(s_t, a_t) * grad log (pi(a_t|s_t))
            boostrapped_returns = rollout.rewards + self.gamma * next_values
            advantages = boostrapped_returns - values 
            policy_loss = -(advantages * log_prob).mean()

            # Value Loss Term: R_t + gamma * V(S_{t+1}) - V(S_t
            # value_loss = advantages.pow(2)
            value_loss = self.value_criterion(boostrapped_returns, values)

            # accumulate gradients for multi-rollout case
            self.loss += self.ac_w * policy_loss + self.cr_w * value_loss


class DQN(RLBaseStrategy):

    def __init__(
            self, model: nn.Module, optimizer: Optimizer,
            per_experience_steps: Union[int, Timestep], 
            rollouts_per_step: int = 8,  # how often do you perform an update step
            replay_memory_size: int = 10000,
            replay_memory_init_size: int = 5000,
            updates_per_step=1,
            criterion=nn.SmoothL1Loss(), 
            batch_size: int = 32,
            initial_epsilon: float = 1.0,
            final_epsilon: float = 0.05,
            exploration_fraction: float = 0.1,
            double_dqn: bool = True,
            target_net_update_interval: Union[int, Timestep] = 10000,
            polyak_update_tau: float = 0.01, # set to 1. to hard copy
            discount_factor: float = 0.99,
            device='cpu',
            plugins: Optional[Sequence[StrategyPlugin]] = [],
            eval_every=-1):
        super().__init__(
            model, optimizer, per_experience_steps, criterion=criterion,
            per_experience_steps=per_experience_steps,
            rollouts_per_step=rollouts_per_step,
            updates_per_step=updates_per_step, device=device, plugins=plugins,
            discount_factor=discount_factor, eval_every=eval_every)
        if type(target_net_update_interval) is int:
            target_net_update_interval = Timestep(target_net_update_interval)

        assert target_net_update_interval.unit == self.per_experience_steps.unit, "You must express the target network interval using the same unit as the training lenght"
        self.replay_memory: ReplayMemory = None
        self.replay_init_size = replay_memory_init_size
        self.replay_size = replay_memory_size
        self.batch_dim = batch_size
        self.double_dqn = double_dqn
        self.target_net_update_interval:Timestep = target_net_update_interval
        self.polyak_update_tau = polyak_update_tau
        assert initial_epsilon >= final_epsilon, "Initial epsilon value must be greater or equal than final one"

        self._init_eps = initial_epsilon
        self.eps = initial_epsilon
        self.final_eps = final_epsilon
        # compute linear decay rate from specified fraction and specified timestep unit
        self.eps_decay = (
            self._init_eps - self.final_eps) / (exploration_fraction * self.per_experience_steps.value) 

        # initialize target network
        self.target_net = copy.deepcopy(self.model)

    def _update_epsilon(self, experience_timestep: int):
        """
            Linearly decrease exploration rate `self.eps` up to `self.final_eps` in a 
            fraction of the total timesteps (`exploration_fraction`).
            This will reset to `self._init_eps` on new experience.
        """
        new_value = self._init_eps - experience_timestep * self.eps_decay
        self.eps = new_value if new_value > self.final_eps else self.final_eps

    def _update_target_network(self, timestep: int):
        # copy over network parameter to fixed target net
        if timestep > 0 and timestep % self.target_net_update_interval.value:
            # from stable baseline 3 enhancement https://github.com/DLR-RM/stable-baselines3/issues/93
            with torch.no_grad():
                # all done in-place for efficiency
                for param, target_param in zip(self.model.parameters(), self.target_net.parameters()):
                    target_param.data.mul_(1-self.polyak_update_tau)
                    torch.add(target_param.data, param.data, alpha=self.polyak_update_tau, out=target_param.data)

    def before_training_exp(self, **kwargs):
        # initialize replay memory with collected data before training on new env
        rollouts = self.rollout(
            self.environment, self.replay_init_size,
            max_steps=self.replay_init_size)
        if self.replay_memory is None:
            self.replay_memory = ReplayMemory(size=self.replay_size)
        self.replay_memory.add_rollouts(rollouts)

    def before_rollout(self, **kwargs):
        # update exploration rate
        self._update_epsilon(self.timestep)
        # update fixed target network
        self._update_target_network(self.timestep)

        return super().before_rollout(**kwargs)

    def after_rollout(self, **kwargs):
        # add collected rollouts to replay memory
        self.replay_memory.add_rollouts(self.rollouts)
        return super().after_rollout(**kwargs)

    def sample_rollout_action(self, observations: torch.Tensor):
        """
            Generate action following epsilon-greedy strategy in which we either sample
            a random action with probability epsilon or we exploit current Q-value derived
            policy by taking the action with greater Q-value.

        Args:
            observations (torch.Tensor): [description]
        """ 
        # all actors interacting with environment either exploit or explore
        if random.random() > self.eps:
            # exploitation
            with torch.no_grad():
                q_values = self.model(observations)
                actions = torch.argmax(q_values, dim=1).cpu().int().numpy()
        else:
            actions = [
                self.environment.action_space.sample()
                for i in range(self.environment.action_space.n)]
            actions = np.asarray(actions, dtype=np.int32)
        # actors run on cpu, return numpy array 
        return actions

    @torch.no_grad()
    def _compute_next_q_values(self, batch):
        # Compute next state q values using fixed target net
        next_q_values = self.target_net(batch.next_observations)

        if self.double_dqn:
            # Q'(s', argmax_a' Q(s', a') ):
            # use model to select the action with maximal value (follow greedy policy with current weights)
            max_actions = torch.argmax(
                self.model(batch.next_observations), dim=1)
            # evaluate q value of that action using fixed target network
            # select max actions, one per batch element
            next_q_values = next_q_values[torch.arange(
                next_q_values.shape[0]), max_actions]
            # equal to torch.gather(next_q_values, dim=1, index=max_actions.unsqueeze(-1))
        else:
            # get values corresponding to highest q value actions
            next_q_values, _ = next_q_values.max(dim=1)

        return next_q_values

    def update(self, rollouts: List[Rollout], n_update_steps: int):
        for i in range(n_update_steps):
            # sample batch of steps/experiences from memory
            batch = self.replay_memory.sample_batch(self.batch_dim)
            # compute q values prediction for whole batch: Q(s, a)
            q_pred = self.model(batch.observations)
            # condition on taken actions (select performed actions' q-values)
            q_pred = torch.gather(
                q_pred, dim=1, index=batch.actions.unsqueeze(-1))

            # compute target Q value: Q*(s, a) = R_t + gamma * max_{a'} Q(s', a') 
            next_q_values = self._compute_next_q_values(batch)

            # mask terminal states only after max q value action has been selected
            q_target = batch.rewards + self.gamma * \
                (1 - batch.dones) * next_q_values

            loss = self._criterion(q_pred, q_target)

            # TODO: gradient norm clipping?