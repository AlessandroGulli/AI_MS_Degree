#!/usr/bin/env python
import sys,os
sys.path.append('./pybullet_robot')
import gym
from gym import error, spaces, utils
from gym.utils import seeding
import numpy as np
import pybullet as pb
from pybullet_robot.worlds import SimpleWorld, add_PyB_models_to_path
from pybullet_robot.robots import PandaArm
import quaternion
from pybullet_robot.controllers.utils import euler_to_quaternion_raw, quatdiff_in_euler, weighted_minkowskian_distance, sample_torus_coordinates, quaternion_to_euler_angle
from pybullet_robot.controllers import OSImpedanceController, OSImpedanceControllerJointSpace
from pybullet_robot.controllers.utils import display_trajectories
from pybullet_robot.controllers.planning import Trajectory_Generator
from pybullet_robot.controllers.traj_config_joints_training import Traj_Config
import time
import pybullet_data
import math
import numpy as np
import random
from typing import Callable, Dict, List, Optional, Tuple, Type, Union
import torch as th
from torch import nn
from stable_baselines3 import PPO
from stable_baselines3.common.env_checker import check_env
from stable_baselines3.common.callbacks import StopTrainingOnMaxEpisodes, StopTrainingOnRewardThreshold
from stable_baselines3.common.evaluation import evaluate_policy
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv, VecNormalize, VecEnv,  sync_envs_normalization
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.results_plotter import load_results, ts2xy, plot_results
from stable_baselines3.common.callbacks import BaseCallback, EventCallback, EvalCallback, CallbackList
from stable_baselines3.common.policies import ActorCriticPolicy
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.utils import set_random_seed
from typing import Callable, Dict, List, Optional, Tuple, Type, Union, Any
import multiprocessing as mp
import datetime
import logging
import optuna
import yaml
from optuna.integration.skopt import SkoptSampler
from optuna.pruners import BasePruner, MedianPruner, SuccessiveHalvingPruner
from optuna.samplers import BaseSampler, RandomSampler, TPESampler
from optuna.visualization import plot_optimization_history
from optuna.visualization import plot_parallel_coordinate
from optuna.visualization import plot_param_importances
from pprintpp import pprint
import pickle as pkl


class CustomEvalCallback(EventCallback):
    """
    Callback for evaluating an agent.
    .. warning::
      When using multiple environments, each call to  ``env.step()``
      will effectively correspond to ``n_envs`` steps.
      To account for that, you can use ``eval_freq = max(eval_freq // n_envs, 1)``
    :param eval_env: The environment used for initialization
    :param callback_on_new_best: Callback to trigger
        when there is a new best model according to the ``mean_reward``
    :param n_eval_episodes: The number of episodes to test the agent
    :param eval_freq: Evaluate the agent every ``eval_freq`` call of the callback.
    :param log_path: Path to a folder where the evaluations (``evaluations.npz``)
        will be saved. It will be updated at each evaluation.
    :param best_model_save_path: Path to a folder where the best model
        according to performance on the eval env will be saved.
    :param deterministic: Whether the evaluation should
        use a stochastic or deterministic actions.
    :param render: Whether to render or not the environment during evaluation
    :param verbose:
    :param warn: Passed to ``evaluate_policy`` (warns if ``eval_env`` has not been
        wrapped with a Monitor wrapper)
    """

    def __init__(
            self,
            eval_env: Union[gym.Env, VecEnv],
            callback_on_new_best: Optional[BaseCallback] = None,
            n_eval_episodes: int = 5,
            eval_freq: int = 10000,
            log_path: Optional[str] = None,
            best_model_save_path: Optional[str] = None,
            deterministic: bool = True,
            render: bool = False,
            verbose: int = 1,
            warn: bool = True,
            ntrial: int = 0,
    ):
        super(CustomEvalCallback, self).__init__(callback_on_new_best, verbose=verbose)
        self.n_eval_episodes = n_eval_episodes
        self.eval_freq = eval_freq
        self.best_mean_reward = -np.inf
        self.last_mean_reward = -np.inf
        self.deterministic = deterministic
        self.render = render
        self.warn = warn
        self.ntrial = ntrial

        # Convert to VecEnv for consistency
        if not isinstance(eval_env, VecEnv):
            eval_env = DummyVecEnv([lambda: eval_env])

        self.eval_env = eval_env
        self.best_model_save_path = best_model_save_path
        # Logs will be written in ``evaluations.npz``
        if log_path is not None:
            log_path = os.path.join(log_path, f"evaluations{self.ntrial}")
        self.log_path = log_path
        self.evaluations_results = []
        self.evaluations_timesteps = []
        self.evaluations_length = []
        # For computing success rate
        self._is_success_buffer = []
        self.evaluations_successes = []

    def _init_callback(self) -> None:
        # Does not work in some corner cases, where the wrapper is not the same
        if not isinstance(self.training_env, type(self.eval_env)):
            warnings.warn("Training and eval env are not of the same type" f"{self.training_env} != {self.eval_env}")

        # Create folders if needed
        if self.best_model_save_path is not None:
            os.makedirs(self.best_model_save_path, exist_ok=True)
        if self.log_path is not None:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def _log_success_callback(self, locals_: Dict[str, Any], globals_: Dict[str, Any]) -> None:
        """
        Callback passed to the  ``evaluate_policy`` function
        in order to log the success rate (when applicable),
        for instance when using HER.
        :param locals_:
        :param globals_:
        """
        info = locals_["info"]

        if locals_["done"]:
            maybe_is_success = info.get("is_success")
            if maybe_is_success is not None:
                self._is_success_buffer.append(maybe_is_success)

    def _on_step(self) -> bool:

        if self.eval_freq > 0 and self.n_calls % self.eval_freq == 0:
            # Sync training and eval env if there is VecNormalize
            if self.model.get_vec_normalize_env() is not None:
                try:
                    sync_envs_normalization(self.training_env, self.eval_env)
                except AttributeError:
                    raise AssertionError(
                        "Training and eval env are not wrapped the same way, "
                        "see https://stable-baselines3.readthedocs.io/en/master/guide/callbacks.html#evalcallback "
                        "and warning above."
                    )

            # Reset success rate buffer
            self._is_success_buffer = []

            episode_rewards, episode_lengths = evaluate_policy(
                self.model,
                self.eval_env,
                n_eval_episodes=self.n_eval_episodes,
                render=self.render,
                deterministic=self.deterministic,
                return_episode_rewards=True,
                warn=self.warn,
                callback=self._log_success_callback,
            )

            if self.log_path is not None:
                self.evaluations_timesteps.append(self.num_timesteps)
                self.evaluations_results.append(episode_rewards)
                self.evaluations_length.append(episode_lengths)

                kwargs = {}
                # Save success log if present
                if len(self._is_success_buffer) > 0:
                    self.evaluations_successes.append(self._is_success_buffer)
                    kwargs = dict(successes=self.evaluations_successes)

                np.savez(
                    self.log_path,
                    timesteps=self.evaluations_timesteps,
                    results=self.evaluations_results,
                    ep_lengths=self.evaluations_length,
                    **kwargs,
                )

            mean_reward, std_reward = np.mean(episode_rewards), np.std(episode_rewards)
            mean_ep_length, std_ep_length = np.mean(episode_lengths), np.std(episode_lengths)
            self.last_mean_reward = mean_reward

            if self.verbose > 0:
                print(
                    f"Eval num_timesteps={self.num_timesteps}, " f"episode_reward={mean_reward:.2f} +/- {std_reward:.2f}")
                print(f"Episode length: {mean_ep_length:.2f} +/- {std_ep_length:.2f}")
            # Add to current Logger
            self.logger.record("eval/mean_reward", float(mean_reward))
            self.logger.record("eval/mean_ep_length", mean_ep_length)

            if len(self._is_success_buffer) > 0:
                success_rate = np.mean(self._is_success_buffer)
                if self.verbose > 0:
                    print(f"Success rate: {100 * success_rate:.2f}%")
                self.logger.record("eval/success_rate", success_rate)

            # Dump log so the evaluation results are printed with the correct timestep
            self.logger.record("time/total_timesteps", self.num_timesteps, exclude="tensorboard")
            self.logger.dump(self.num_timesteps)

            stats_path = os.path.join(self.best_model_save_path, f"vec_normalize_best{self.ntrial}.pkl")
            self.eval_env.save(stats_path)

            if mean_reward > self.best_mean_reward:
                if self.verbose > 0:
                    print("New best mean reward!")
                if self.best_model_save_path is not None:
                    self.model.save(os.path.join(self.best_model_save_path, f"best_model{self.ntrial}"))
                self.best_mean_reward = mean_reward

                # Trigger callback if needed
                if self.callback is not None:
                    return self._on_event()

        return True

    def update_child_locals(self, locals_: Dict[str, Any]) -> None:
        """
        Update the references to the local variables.
        :param locals_: the local variables during rollout collection
        """
        if self.callback:
            self.callback.update_locals(locals_)


class PandaEnv(gym.Env):
    metadata = {'render.modes': ['human']}

    def __init__(self, ntrial,  render_enable=False):
        super(PandaEnv, self).__init__()

        self.robot = PandaArm(uid="DIRECT")
        self.planning = Trajectory_Generator(Traj_Config)
        self.ntrial = ntrial

        add_PyB_models_to_path()

        # Action space
        # q_dot_target -> 7 items, one for each joint
        n_joints = 7
        total_actions = n_joints
        self.action_space = spaces.Box(np.array([-1] * total_actions), np.array([1] * total_actions))

        # Observation space
        # delta_x -> 6 items, 3 for position and 3 for orientation
        delta_cartesian_pos_ori = 6
        # q       -> 7 items, one for each joint
        joint_angles = 7
        # q_dot   -> 7 items, one for each joint
        joint_velocities = 7

        total_observations = delta_cartesian_pos_ori + joint_angles + joint_velocities
        self.observation_space = spaces.Box(np.array([-np.inf] * total_observations),
                                            np.array([np.inf] * total_observations))

        # Robot initial poses
        self.default_pose = np.asarray(
            [0.31058202, 0.00566847, 0.58654213, -3.1240766304753516, 0.04029881344985077, 0.0288575068115082])

        self.home_pose = np.concatenate([np.asarray([0.45, 0, 0.5]), self.planning._points[0][3:6]])
        self.roll_out_state = {'Change_Target': True, 'Target': self.home_pose}
        self.reset_pose = self.home_pose
        self.stateId = -1

        # Torus Parameters
        self.R = 0.45
        self.r = 0.15
        self.angle = np.pi / 8

        self.init_logger = True

    def reset_to_start_pose(self, points, T, NPoints, gripper_cmd, world):
        x_e, dx_e, g, t = self.planning.path_assembly_from_arguments(points, T, NPoints, gripper_cmd)
        self.planning.execute_joints_trajectory_explicitly(x_e, dx_e, g, world, self.planning._rate, pb)

        print(f'Robot Repositioning Complete: {self.robot.ee_pose()}')

    def reset(self):

        self.robot.reset()
        add_PyB_models_to_path()

        # Create world
        plane = pb.loadURDF('plane.urdf')
        table = pb.loadURDF('table/table.urdf', useFixedBase=True, globalScaling=0.5)
        cube = pb.loadURDF('cube_small.urdf', globalScaling=1.)

        pb.resetBasePositionAndOrientation(table, [0.4, -1.5, 0.0], [0, 0, -0.707, 0.707])

        self.red = [0.97, 0.25, 0.25, 1]
        self.green = [0.41, 0.68, 0.31, 1]
        self.yellow = [0.92, 0.73, 0, 1]
        self.blue = [0, 0.55, 0.81, 1]

        pb.changeVisualShape(cube, -1, rgbaColor=self.red)
        pb.resetBasePositionAndOrientation(cube, [0.4, 0.5, 0.5], [0, 0, -0.707, 0.707])

        objects = {'plane': plane,
                   'table': table,
                   'cube': cube}

        # Assembly world
        self.world = SimpleWorld(self.robot, objects)
        self.world.robot.set_ctrl_mode('tor')
        self.world.robot.gripper_close(pos=0., force=0.)

        if self.init_logger == True:
            formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
            handler = logging.FileHandler(f"./RL/models/Events{self.ntrial}.log")
            handler.setFormatter(formatter)

            logger = logging.getLogger(f'logger_trial{self.ntrial}')
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)

            self.logger = logger
            self.init_logger = False

        # Reset Robot to a given start position
        if self.stateId == 0:
            pb.restoreState(fileName="state.bullet")
        '''
        T = np.array([1.5])
        NPoints = T*self.planning._rate
        gripper_cmd = np.array([-1,0,0]) 
        points = np.array([[self.default_pose[0], self.default_pose[1], self.default_pose[2], self.default_pose[3], self.default_pose[4], self.default_pose[5]], 
			   [self.reset_pose[0]  , self.reset_pose[1]  , self.reset_pose[2]  , self.reset_pose[3]  , self.reset_pose[4]  , self.reset_pose[5]]]) 


        self.reset_to_start_pose(points, T, NPoints, gripper_cmd, self.world)
        '''

        pb.configureDebugVisualizer(pb.COV_ENABLE_RENDERING, 1)  # rendering's back on again

        # Sample from torus region
        if self.roll_out_state['Change_Target'] == True:
            self.roll_out_state['Target'] = sample_torus_coordinates(self.r, self.R, self.angle, 1)
            self.roll_out_state['Change_Target'] = False
        target_ori = self.planning._points[0][3:6]

        curr_pos, curr_ori = self.robot.ee_pose()
        goal_ori = euler_to_quaternion_raw(pb, target_ori)
        goal_ori = np.quaternion(goal_ori[3], goal_ori[0], goal_ori[1], goal_ori[2])
        delta_pos = np.asarray(self.roll_out_state['Target']).reshape([3, 1]) - curr_pos.reshape([3, 1])
        delta_ori = quatdiff_in_euler(curr_ori, goal_ori).reshape([3, 1])
        delta_x = np.concatenate([delta_pos, delta_ori])

        self.goal_joint_angles = np.asarray(self.robot.angles()).reshape([9, 1])
        self.goal_joint_velocities = np.asarray(self.robot.joint_velocities()).reshape([9, 1])
        self.prev_joint_velocities = np.asarray(self.robot.joint_velocities()).reshape([9, 1])

        self.start_time = time.time()

        observation = np.concatenate([delta_x.reshape([6, 1]), self.goal_joint_angles[0:7].reshape([7, 1]),
                                      self.goal_joint_velocities[0:7].reshape([7, 1])]).reshape(-1)

        return observation

    def step(self, action):

        begin_step = time.time()

        pb.addUserDebugLine(self.robot.ee_pose()[0], self.roll_out_state['Target'], lineColorRGB=self.yellow[0:3], lineWidth=2.0, lifeTime=0, physicsClientId=self.robot._uid)

        action = np.concatenate([action, [0., 0.]])

        tmp_goal_joint_velocities_normalized = np.asarray(action).reshape([9, 1])
        limits = np.asarray(self.robot.get_joint_velocity_limits()).reshape([9, 1])
        # normalize back to controller
        tmp_goal_joint_velocities = limits * tmp_goal_joint_velocities_normalized
        # Target velocity integration --> target angles
        tmp_goal_joint_angles = np.add(self.goal_joint_angles, (1. / self.planning._rate) * tmp_goal_joint_velocities)
        tmp_goal_joint_angles = np.asarray(
            np.clip(tmp_goal_joint_angles.reshape(-1), a_min=self.robot.get_joint_limits()['lower'],
                    a_max=self.robot.get_joint_limits()['upper'])).reshape([9, 1])

        # print(f'Angles: {tmp_goal_joint_angles}\nVelocities: {tmp_goal_joint_velocities}')

        """
        Actual control loop. Uses goal pose from the feedback thread
        and current robot states from the subscribed messages to compute
        task-space force, and then the corresponding joint torques.
        """

        curr_joint_angles = np.asarray(self.robot.angles()).reshape([9, 1])
        curr_joint_velocities = np.asarray(self.robot.joint_velocities()).reshape([9, 1])
        # joint accelerations
        curr_joint_accelerations = (curr_joint_velocities - self.prev_joint_velocities) * self.planning._rate
        self.prev_joint_velocities = curr_joint_velocities

        delta_angles = tmp_goal_joint_angles - curr_joint_angles
        delta_velocities = tmp_goal_joint_velocities - curr_joint_velocities

        # Desired joint-space torque using PD law
        kP = np.asarray([1000., 1000., 1000., 1000., 1000., 1000., 500., 0., 0.])
        kD = np.asarray([2., 2., 2., 2, 2., 2, 1., 0., 0.])
        error_thresh = np.asarray([0.010, 0.010])
        # Output controller
        tau = np.add(kP * (delta_angles).T, kD * (delta_velocities).T).reshape(-1)
        error_angles = np.asarray([np.linalg.norm(delta_angles), np.linalg.norm(delta_velocities)])

        # joint torques to be commanded
        torque_cmd = tau + self.robot.torque_compensation()

        # Set goal angles
        self.goal_joint_angles = tmp_goal_joint_angles

        string_debug = ''
        # Sample from torus region
        if self.roll_out_state['Change_Target'] == True:
            self.roll_out_state['Target'] = sample_torus_coordinates(self.r, self.R, self.angle, 1)
            self.roll_out_state['Change_Target'] = False
            string_debug = f"Target changed: {self.roll_out_state['Target']}"
            print(string_debug)
        target_ori = self.planning._points[0][3:6]

        ## Check respect to final pose
        curr_pos, curr_ori = self.robot.ee_pose()
        goal_ori = euler_to_quaternion_raw(pb, target_ori)
        goal_ori = np.quaternion(goal_ori[3], goal_ori[0], goal_ori[1], goal_ori[2])
        delta_pos = np.asarray(self.roll_out_state['Target']).reshape([3, 1]) - curr_pos.reshape([3, 1])
        delta_ori = quatdiff_in_euler(curr_ori, goal_ori).reshape([3, 1])
        delta_x = np.concatenate([delta_pos, delta_ori])
        # Compute errors
        err_pos = np.linalg.norm(delta_pos)
        err_ori = np.linalg.norm(delta_ori)
        global_target_error = np.asarray([err_pos, err_ori])

        # Check rollout status
        total_time = time.time() - self.start_time
        if np.any(global_target_error > error_thresh):
            self.robot.exec_torque_cmd(torque_cmd)
            self.robot.step_if_not_rtsim()
            done = False
            now = datetime.datetime.now()
            if err_pos >= 0.8:
                self.reset_pose = self.home_pose
                self.stateId = -1
                string_debug = f"Home Position Reset, dist from target: {err_pos}"
                done = True
            elif total_time >= 8:
                # self.reset_pose = np.concatenate([self.robot.ee_pose()[0], quaternion_to_euler_angle(pb, self.robot.ee_pose()[1].w,self.robot.ee_pose()[1].x,self.robot.ee_pose()[1].y,self.robot.ee_pose()[1].z)])
                # self.reset_pose = np.concatenate([self.robot.ee_pose()[0], target_ori])
                self.stateId = 0
                pb.saveBullet("state.bullet")
                string_debug = f"Last Position Reset, dist from target: {err_pos}"
                done = True
        elif np.all(global_target_error <= error_thresh):
            self.roll_out_state['Change_Target'] = True
            done = False
            now = datetime.datetime.now()
            string_debug = f"Target reached {self.roll_out_state['Target']} on {now.day}/{now.month}/{now.year} at {now.hour}:{now.minute}:{now.second}"
            print(string_debug)

            # Debug logs
        if len(string_debug) > 0:
            self.logger.debug(string_debug)

            # Apply delay
        elapsed_time = time.time() - begin_step
        sleep_time = (1. / self.planning._rate) - elapsed_time
        if sleep_time > 0.0:
            time.sleep(sleep_time)

            # Get observations
        observation = np.concatenate([delta_x.reshape([6, 1]), self.robot.angles()[0:7].reshape([7, 1]),
                                      self.robot.joint_velocities()[0:7].reshape([7, 1])]).reshape(-1)

        # Compute reward
        lamba_err = 1.
        lamba_eff = 0.  # 0.005
        # delta_x_error = np.square(err_pos)
        delta_x_error = weighted_minkowskian_distance(delta_pos, delta_ori, w_rot=1.0)
        acc_error = np.sqrt(np.sum(np.square(curr_joint_accelerations)))
        # reward = np.exp(-lamba_err*delta_x_error) - lamba_eff*acc_error
        reward = -lamba_err * delta_x_error - lamba_eff * acc_error
        info = {}

        # pb.removeAllUserDebugItems()

        return observation, reward, done, info

    def render(self, mode='human'):
        view_matrix = pb.computeViewMatrixFromYawPitchRoll(cameraTargetPosition=[0.7, 0, 0.05], distance=.7, yaw=90,
                                                           pitch=-70, roll=0, upAxisIndex=2)
        proj_matrix = pb.computeProjectionMatrixFOV(fov=60, aspect=float(960) / 720, nearVal=0.1, farVal=100.0)
        (_, _, px, _, _) = pb.getCameraImage(width=960, height=720, viewMatrix=view_matrix,
                                             projectionMatrix=proj_matrix, renderer=pb.ER_BULLET_HARDWARE_OPENGL)

        rgb_array = np.array(px, dtype=np.uint8)
        rgb_array = np.reshape(rgb_array, (720, 960, 4))
        rgb_array = rgb_array[:, :, :3]

        return rgb_array

    def close(self):
        self.robot.__del__


def linear_schedule(initial_value: Union[float, str]) -> Callable[[float], float]:
    """
    Linear learning rate schedule.
    :param initial_value: (float or str)
    :return: (function)
    """
    if isinstance(initial_value, str):
        initial_value = float(initial_value)

    def func(progress_remaining: float) -> float:
        """
        Progress will decrease from 1 (beginning) to 0
        :param progress_remaining: (float)
        :return: (float)
        """
        return progress_remaining * initial_value

    return func


def sample_ppo_params(trial: optuna.Trial) -> Dict[str, Any]:
    """
    Sampler for PPO hyperparams.
    :param trial:
    :return:
    """
    batch_size = trial.suggest_categorical("batch_size", [8, 16, 32, 64, 128, 256, 512])
    factor_size = trial.suggest_categorical("factor_size", [1, 2, 3, 4, 5])
    n_steps = factor_size*batch_size #trial.suggest_categorical("n_steps", factor_size*batch_size)
    gamma = trial.suggest_categorical("gamma", [0.9, 0.95, 0.98, 0.99, 0.995, 0.999, 0.9999])
    learning_rate = trial.suggest_loguniform("learning_rate", 1e-5, 1)
    lr_schedule = "constant"
    # Uncomment to enable learning rate schedule
    #lr_schedule = trial.suggest_categorical('lr_schedule', ['linear', 'constant'])
    ent_coef = trial.suggest_loguniform("ent_coef", 0.00000001, 0.1)
    clip_range = trial.suggest_categorical("clip_range", [0.1, 0.2, 0.3, 0.4])
    n_epochs = trial.suggest_categorical("n_epochs", [1, 5, 10, 20])
    gae_lambda = trial.suggest_categorical("gae_lambda", [0.8, 0.9, 0.92, 0.95, 0.98, 0.99, 1.0])
    max_grad_norm = trial.suggest_categorical("max_grad_norm", [0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1, 2, 5])
    vf_coef = trial.suggest_uniform("vf_coef", 0, 1)
    net_arch = trial.suggest_categorical("net_arch", ["small", "medium", "large"])
    # Uncomment for gSDE (continuous actions)
    #log_std_init = trial.suggest_uniform("log_std_init", -4, 1)
    # Uncomment for gSDE (continuous action)
    #sde_sample_freq = trial.suggest_categorical("sde_sample_freq", [-1, 8, 16, 32, 64, 128, 256])
    # Orthogonal initialization
    #ortho_init = False
    ortho_init = trial.suggest_categorical('ortho_init', [False, True])
    # activation_fn = trial.suggest_categorical('activation_fn', ['tanh', 'relu', 'elu', 'leaky_relu'])
    activation_fn = trial.suggest_categorical("activation_fn", ["tanh", "relu"])

    # TODO: account when using multiple envs
    if batch_size > n_steps:
        batch_size = n_steps

    if lr_schedule == "linear":
        learning_rate = linear_schedule(learning_rate)

    # Independent networks usually work best
    # when not working with images
    net_arch = {
        "small": [dict(pi=[64, 64], vf=[64, 64])],
        "medium": [dict(pi=[128, 128], vf=[128, 128])],
        "large": [dict(pi=[256, 256], vf=[256, 256])]
    }[net_arch]

    activation_fn = {"tanh": nn.Tanh, "relu": nn.ReLU, "elu": nn.ELU, "leaky_relu": nn.LeakyReLU}[activation_fn]

    return {
        "n_steps": n_steps,
        "batch_size": batch_size,
        "gamma": gamma,
        "learning_rate": learning_rate,
        "ent_coef": ent_coef,
        "clip_range": clip_range,
        "n_epochs": n_epochs,
        "gae_lambda": gae_lambda,
        "max_grad_norm": max_grad_norm,
        "vf_coef": vf_coef,
        #"sde_sample_freq": sde_sample_freq,
        "policy_kwargs": dict(
            #log_std_init=log_std_init,
            net_arch=net_arch,
            activation_fn=activation_fn,
            ortho_init=ortho_init,
        ),
    }

def optimize_agent(trial):

    # If the environment don't follow the interface, an error will be thrown
    #check_env(PandaEnv(), warn=True)

    # Create log dir
    log_dir = './RL/models/'
    os.makedirs(log_dir, exist_ok=True)

    """ Train the model and optimize
        Optuna maximises the negative log likelihood, so we
        need to negate the reward here
    """
    model_params = sample_ppo_params(trial)

    env = PandaEnv(trial.number)
    env = DummyVecEnv([lambda: env])
    # Automatically normalize the input features and reward
    env = VecNormalize(env, norm_obs=True, norm_reward=True, clip_obs=10.)
    # Stops training when the model reaches the maximum number of episodes
    callback_max_episodes = StopTrainingOnMaxEpisodes(max_episodes=20000, verbose=3)
    callback_on_reward = StopTrainingOnRewardThreshold(reward_threshold=10000, verbose=3)
    eval_callback_stop = EvalCallback(env, callback_on_new_best=callback_on_reward, verbose=3)
    # Use continuos actions for evaluation
    eval_callback_best = CustomEvalCallback(env, best_model_save_path=log_dir, log_path=log_dir, n_eval_episodes=5, eval_freq=1000, deterministic=True, render=False, ntrial=trial.number)

    callback = CallbackList([callback_max_episodes, eval_callback_stop, eval_callback_best])

    model = PPO("MlpPolicy", env, verbose=2, tensorboard_log="./RL/panda_PPO_tensorboard/", **model_params)
    n_timesteps = 3e05
    try:
        model.learn(int(n_timesteps), callback=callback, tb_log_name="telemetry")
        mean_reward, _ = evaluate_policy(model, env, n_eval_episodes=10)
        model.save(os.path.join(log_dir, f"final_model{trial.number}"))
        stats_path = os.path.join(log_dir, f"vec_normalize_final{trial.number}.pkl")
        env.save(stats_path)
        # Free memory
        model.env.close()
    except (AssertionError, ValueError) as e:
        # Sometimes, random hyperparams can generate NaN
        # Free memory
        model.env.close()
        # Prune hyperparams that generate NaNs
        print(e)
        print("============")
        print("Sampled hyperparams:")
        pprint(model_params)
        raise optuna.exceptions.TrialPruned()

    del model.env
    del model

    return mean_reward


if __name__ == '__main__':

    optim_plots = True
    verbose = 1

    n_timesteps = 3e05

    optuna.logging.get_logger("optuna").addHandler(logging.StreamHandler(sys.stdout))
    study_name = "ppo-study"
    storage_name = "sqlite:///{}.db".format(study_name)
    study = optuna.create_study(study_name=study_name, storage=storage_name,  sampler=RandomSampler(seed=0), pruner=MedianPruner(n_startup_trials=0, n_warmup_steps=1 // 3), direction="maximize")

    try:
        study.optimize(optimize_agent, n_trials=50, n_jobs=1, gc_after_trial=True)

        print("Number of finished trials: ", len(study.trials))

        print("Best trial:")
        trial = study.best_trial

        print("Value: ", trial.value)

        print("Params: ")
        for key, value in trial.params.items():
            print(f"    {key}: {value}")

        report_name = (
            f"report_PandaEnV_{trial.number}-trials-{int(n_timesteps)}"
            f"-RandomSampler-MedianPruner_{int(time.time())}"
        )

        # Create log dir
        log_dir = './RL/models/'
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, 'study_report', report_name)

        if verbose:
            print(f"Writing report to {log_path}")

        # Write report
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        study.trials_dataframe().to_csv(f"{log_path}.csv")

        # Save python object to inspect/re-use it later
        with open(f"{log_path}.pkl", "wb+") as f:
            pkl.dump(study, f)

        # Skip plots
        if optim_plots:
            # Plot optimization result
            try:
                fig1 = plot_optimization_history(study)
                fig2 = plot_param_importances(study)

                fig1.show()
                fig2.show()
            except (ValueError, ImportError, RuntimeError):
                pass

    except KeyboardInterrupt:
        print('Interrupted by keyboard.')
