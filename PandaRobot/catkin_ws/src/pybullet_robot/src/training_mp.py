#!/usr/bin/env python
import sys,os
from typing import Any, Callable, Dict, Optional, Type, Union
sys.path.append('./pybullet_robot')
import gym
from gym import error, spaces, utils
from gym.utils import seeding
import numpy as np
import pybullet as pb
from pybullet_robot.worlds import SimpleWorld, add_PyB_models_to_path
from pybullet_robot.robots import PandaArm, PandaArmMP
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
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.utils import set_random_seed
import multiprocessing as mp
import datetime
import logging


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
    ):
        super(CustomEvalCallback, self).__init__(callback_on_new_best, verbose=verbose)
        self.n_eval_episodes = n_eval_episodes
        self.eval_freq = eval_freq
        self.best_mean_reward = -np.inf
        self.last_mean_reward = -np.inf
        self.deterministic = deterministic
        self.render = render
        self.warn = warn

        # Convert to VecEnv for consistency
        if not isinstance(eval_env, VecEnv):
            eval_env = DummyVecEnv([lambda: eval_env])

        self.eval_env = eval_env
        self.best_model_save_path = best_model_save_path
        # Logs will be written in ``evaluations.npz``
        if log_path is not None:
            log_path = os.path.join(log_path, "evaluations")
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
                print(f"Eval num_timesteps={self.num_timesteps}, " f"episode_reward={mean_reward:.2f} +/- {std_reward:.2f}")
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
            
            stats_path = os.path.join(self.best_model_save_path, "vec_normalize_best.pkl")
            self.eval_env.save(stats_path)

            if mean_reward > self.best_mean_reward:
                if self.verbose > 0:
                    print("New best mean reward!")
                if self.best_model_save_path is not None:
                    self.model.save(os.path.join(self.best_model_save_path, "best_model"))
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


class PandaWrapper(gym.Wrapper):

    def __init__(self, env):              
 
        #Rewards
        env.reward_range = (-np.inf, np.inf)
        
        super(PandaWrapper, self).__init__(env)

    def reset(self):
        return self.env.reset()
    
    def step(self, action):        
        observation, reward, done, info = self.env.step(action)        
        return observation, reward, done, info     


class PandaEnv(gym.Env):

    metadata = {'render.modes':['human']}

    def __init__(self, render_enable = False): 
        super(PandaEnv, self).__init__()
 
        self.robot = PandaArmMP(uid="DIRECT")        
        self.planning = Trajectory_Generator(Traj_Config)

        #add_PyB_models_to_path()              

        # Action space
        # q_dot_target -> 7 items, one for each joint 
        n_joints = 7
        total_actions = n_joints            
        self.action_space = spaces.Box(np.array([-1]*total_actions), np.array([1]*total_actions))

        # Observation space
        # delta_x -> 6 items, 3 for position and 3 for orientation 
        delta_cartesian_pos_ori = 6
        # q       -> 7 items, one for each joint
        joint_angles = 7
        # q_dot   -> 7 items, one for each joint
        joint_velocities = 7

        total_observations = delta_cartesian_pos_ori + joint_angles + joint_velocities
        self.observation_space = spaces.Box(np.array([-np.inf]*total_observations), np.array([np.inf]*total_observations))
        
        # Robot initial poses
        self.default_pose = np.asarray([0.31058202, 0.00566847, 0.58654213, -3.1240766304753516, 0.04029881344985077, 0.0288575068115082 ])

        self.home_pose = np.concatenate([np.asarray([0.45, 0, 0.5]), self.planning._points[0][3:6]])
        self.roll_out_state = {'Change_Target': True, 'Target': self.home_pose}
        self.reset_pose = self.home_pose
        self.stateId = -1
        
        # Torus Parameters
        self.R = 0.45
        self.r = 0.15
        self.angle = np.pi/8

        self.init_logger = True
        
        
    def reset_to_start_pose(self, points, T, NPoints, gripper_cmd, world):        
        x_e, dx_e, g, t = self.planning.path_assembly_from_arguments(points, T, NPoints, gripper_cmd)         
        self.planning.execute_joints_trajectory_explicitly(x_e, dx_e, g, world, self.planning._rate, self.robot.pb)
        
        print(f'Robot Repositioning Complete: {self.robot.ee_pose()}')
 
 
    def reset(self):

        self.robot.reset()
        #add_PyB_models_to_path()  

        # Create world
        plane = self.robot.pb.loadURDF('plane.urdf')
        table = self.robot.pb.loadURDF('table/table.urdf', useFixedBase=True, globalScaling=0.5)                      
        cube = self.robot.pb.loadURDF('cube_small.urdf', globalScaling=1.)
        
        self.robot.pb.resetBasePositionAndOrientation(table, [0.4, -1.5, 0.0], [0, 0, -0.707, 0.707])
        
        self.red    = [0.97, 0.25, 0.25, 1]
        self.green  = [0.41, 0.68, 0.31, 1]
        self.yellow = [0.92, 0.73, 0, 1]
        self.blue   = [0, 0.55, 0.81, 1] 
        
        self.robot.pb.changeVisualShape(cube,-1,rgbaColor=self.red)   
        self.robot.pb.resetBasePositionAndOrientation(cube, [0.4, 0.5, 0.5], [0, 0, -0.707, 0.707])
      
        objects = {'plane': plane, 
                   'table': table,              
                   'cube' : cube} 
                             
        # Assembly world
        self.world = SimpleWorld(self.robot, objects)
        self.world.robot.set_ctrl_mode('tor') 
        self.world.robot.gripper_close(pos = 0., force= 0.)

        if self.init_logger == True:

            formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
            handler = logging.FileHandler("./RL/models/Events" + str(self.robot._uid) + ".log")
            handler.setFormatter(formatter)

            logger = logging.getLogger('logger'+str(self.robot._uid))
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)

            self.logger = logger
            self.init_logger = False
        
        # Reset Robot to a given start position
        if self.stateId == 0:        	
        	self.robot.pb.restoreState(fileName="state.bullet"+str(self.robot._uid))
        '''
        T = np.array([1.5])
        NPoints = T*self.planning._rate
        gripper_cmd = np.array([-1,0,0]) 
        points = np.array([[self.default_pose[0], self.default_pose[1], self.default_pose[2], self.default_pose[3], self.default_pose[4], self.default_pose[5]], 
			   [self.reset_pose[0]  , self.reset_pose[1]  , self.reset_pose[2]  , self.reset_pose[3]  , self.reset_pose[4]  , self.reset_pose[5]]]) 
        self.reset_to_start_pose(points, T, NPoints, gripper_cmd, self.world)
        '''
 
        self.robot.pb.configureDebugVisualizer(self.robot.pb.COV_ENABLE_RENDERING,1) # rendering's back on again
        
        # Sample from torus region
        if self.roll_out_state['Change_Target'] == True:
            self.roll_out_state['Target'][0:3] = sample_torus_coordinates(self.r, self.R, self.angle, 1)
            self.roll_out_state['Target'][3:6] = self.planning._points[0][3:6]
            self.roll_out_state['Change_Target'] = False        	

        curr_pos, curr_ori = self.robot.ee_pose()
        goal_ori = euler_to_quaternion_raw(self.robot.pb, elf.roll_out_state['Target'][3:6])
        goal_ori = np.quaternion(goal_ori[3], goal_ori[0], goal_ori[1], goal_ori[2]) 
        delta_pos = np.asarray(self.roll_out_state['Target'][0:3]).reshape([3,1]) - curr_pos.reshape([3,1])      
        delta_ori = quatdiff_in_euler(curr_ori, goal_ori).reshape([3,1])  
        delta_x = np.concatenate([delta_pos, delta_ori])  
        
        self.goal_joint_angles = np.asarray(self.robot.angles()).reshape([9,1])
        self.goal_joint_velocities = np.asarray(self.robot.joint_velocities()).reshape([9,1])
        self.prev_joint_velocities = np.asarray(self.robot.joint_velocities()).reshape([9,1])

        self.start_time = time.time()

        observation = np.concatenate([ delta_x.reshape([6,1]), self.goal_joint_angles[0:7].reshape([7,1]), self.goal_joint_velocities[0:7].reshape([7,1]) ]).reshape(-1)

        return observation

    def step(self, action):

        begin_step = time.time()
        
        self.robot.pb.addUserDebugLine(self.robot.ee_pose()[0], self.roll_out_state['Target'][0:3], lineColorRGB=self.yellow[0:3], lineWidth=2.0, lifeTime=0, physicsClientId=self.robot._uid) 
        
        action = np.concatenate([action, [0., 0.]])
        
        tmp_goal_joint_velocities_normalized = np.asarray(action).reshape([9,1])        
        limits = np.asarray(self.robot.get_joint_velocity_limits()).reshape([9,1])
        # normalize back to controller        
        tmp_goal_joint_velocities = limits*tmp_goal_joint_velocities_normalized
        # Target velocity integration --> target angles
        tmp_goal_joint_angles =  np.add(self.goal_joint_angles, (1./self.planning._rate)*tmp_goal_joint_velocities)
        tmp_goal_joint_angles = np.asarray(np.clip(tmp_goal_joint_angles.reshape(-1), a_min=self.robot.get_joint_limits()['lower'], a_max=self.robot.get_joint_limits()['upper'])).reshape([9, 1])

        #print(f'Angles: {tmp_goal_joint_angles}\nVelocities: {tmp_goal_joint_velocities}')

        """
        Actual control loop. Uses goal pose from the feedback thread
        and current robot states from the subscribed messages to compute
        task-space force, and then the corresponding joint torques.
        """
          
        curr_joint_angles =  np.asarray(self.robot.angles()).reshape([9,1])
        curr_joint_velocities =  np.asarray(self.robot.joint_velocities()).reshape([9,1])
        #joint accelerations
        curr_joint_accelerations = (curr_joint_velocities - self.prev_joint_velocities)*self.planning._rate
        self.prev_joint_velocities = curr_joint_velocities
              
        delta_angles     = tmp_goal_joint_angles - curr_joint_angles
        delta_velocities = tmp_goal_joint_velocities - curr_joint_velocities

        # Desired joint-space torque using PD law
        kP = np.asarray([1000., 1000., 1000., 1000., 1000., 1000., 500., 0., 0.])    
        kD =  np.asarray([2.   , 2.   , 2.  , 2  , 2.  , 2 , 1. , 0., 0.])
        error_thresh = np.asarray([0.010, 0.010])
        # Output controller
        tau = np.add(kP*(delta_angles).T, kD*(delta_velocities).T).reshape(-1)            
        error_angles = np.asarray([np.linalg.norm(delta_angles), np.linalg.norm(delta_velocities)])
        
        # joint torques to be commanded
        torque_cmd = tau + self.robot.torque_compensation()
                  
        # Set goal angles
        self.goal_joint_angles = tmp_goal_joint_angles             
        
        string_debug =''
        # Sample from torus region
        if self.roll_out_state['Change_Target'] == True:
        	self.roll_out_state['Target'][0:3] = sample_torus_coordinates(self.r, self.R, self.angle, 1)
        	self.roll_out_state['Target'][3:6] = self.planning._points[0][3:6] 
        	self.roll_out_state['Change_Target'] = False
        	string_debug = f"Target changed: {self.roll_out_state['Target']}"
        	print(string_debug)
        
        ## Check respect to final pose
        curr_pos, curr_ori = self.robot.ee_pose()
        goal_ori = euler_to_quaternion_raw(self.robot.pb, target_ori)
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
            elif total_time >= 10:                             
               #self.reset_pose = np.concatenate([self.robot.ee_pose()[0], quaternion_to_euler_angle(pb, self.robot.ee_pose()[1].w,self.robot.ee_pose()[1].x,self.robot.ee_pose()[1].y,self.robot.ee_pose()[1].z)])
               #self.reset_pose = np.concatenate([self.robot.ee_pose()[0], target_ori])
               self.stateId = 0
               self.robot.pb.saveBullet("state.bullet"+str(self.robot._uid))               
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
        sleep_time = (1./self.planning._rate) - elapsed_time        
        if sleep_time > 0.0:
            time.sleep(sleep_time) 
            
        # Get observations
        observation = np.concatenate([ delta_x.reshape([6,1]), self.robot.angles()[0:7].reshape([7,1]), self.robot.joint_velocities()[0:7].reshape([7,1]) ]).reshape(-1) 
        
        # Compute reward        
        lamba_err = 1.
        lamba_eff = 0.     
                
        distance = "weighted"
        if distance == "weighted":               
              delta_x_error = weighted_minkowskian_distance(delta_pos, delta_ori, w_rot=1.0)
              acc_error = np.sqrt(np.sum(np.square(curr_joint_accelerations)))
              #reward = np.exp(-lamba_err*delta_x_error) - lamba_eff*acc_error # Nvidia
              reward = -lamba_err*delta_x_error - lamba_eff*acc_error                
        else:      
             X, Y, Z, X_target, Y_target, Z_target = calc_reference_frames(self.world, self.roll_out_state['Target'][0:3], self.roll_out_state['Target'][3:6], pb)        
             delta_x = np.asarray(X_target.reshape(3,1) - X.reshape(3,1))
             err_x = np.linalg.norm(delta_x)
             delta_y = np.asarray(Y_target.reshape(3,1) - Y.reshape(3,1))
             err_y = np.linalg.norm(delta_y)
             delta_z = np.asarray(Z_target.reshape(3,1) - Z.reshape(3,1))
             err_z = np.linalg.norm(delta_z)
                
             delta_x_error = err_x + err_y + err_z + err_pos        
             reward = -lamba_err*(delta_x_error)  
        
        info = {} 
        
        #pb.removeAllUserDebugItems()
        
        return observation, reward, done, info


    def render(self, mode='human'):
        view_matrix = self.robot.pb.computeViewMatrixFromYawPitchRoll(cameraTargetPosition=[0.7,0,0.05], distance=.7, yaw=90, pitch=-70, roll=0, upAxisIndex=2)                              
        proj_matrix = self.robot.pb.computeProjectionMatrixFOV(fov=60, aspect=float(960) /720, nearVal=0.1, farVal=100.0)
        (_, _, px, _, _) = self.robot.pb.getCameraImage(width=960, height=720, viewMatrix=view_matrix, projectionMatrix=proj_matrix, renderer=self.robot.pb.ER_BULLET_HARDWARE_OPENGL)

        rgb_array = np.array(px, dtype=np.uint8)
        rgb_array = np.reshape(rgb_array, (720,960, 4))
        rgb_array = rgb_array[:, :, :3]
                
        return rgb_array
	
    def close(self):
        self.robot.__del__        

if __name__ == '__main__':


    check_env(PandaEnv(), warn=True)

    # Create log dir
    log_dir = './RL/models/'
    os.makedirs(log_dir, exist_ok=True)  
    
    #env_id = PandaWrapper
    #env_kwargs = dict(env=PandaEnv())
    # Create the agent
    n_envs = 4  # Number of processes to use
    # Create the vectorized environment            
    #env = make_vec_env(env_id, n_envs=n_envs, env_kwargs=env_kwargs)
    env = make_vec_env(PandaEnv, n_envs=n_envs)
    # Automatically normalize the input features and reward
    env = VecNormalize(env, norm_obs=True, norm_reward=True, clip_obs=10.)        
    # Custom actor (pi) and value function (vf) networks
    # of three layers of size 128 each with Tanh activation function
    policy_kwargs = dict(activation_fn=th.nn.ReLU, ortho_init=False, log_std_init=-2, net_arch=[dict(pi=[256, 256], vf=[256, 256])])
    # Stops training when the model reaches the maximum number of episodes
    callback_max_episodes = StopTrainingOnMaxEpisodes(max_episodes=20000, verbose=3)    
    # Use continuos actions for evaluation    
    eval_callback_best = CustomEvalCallback(env, best_model_save_path=log_dir, log_path=log_dir, n_eval_episodes=2, eval_freq=500, deterministic=True, render=False)
    callback_on_reward = StopTrainingOnRewardThreshold(reward_threshold=10000, verbose = 3)
    eval_callback_stop = EvalCallback(env, callback_on_new_best=callback_on_reward, verbose=3)
    callback = CallbackList([callback_max_episodes, eval_callback_best, eval_callback_stop])
    
    model = PPO("MlpPolicy", env, batch_size=128, n_steps=512, n_epochs=20, gae_lambda=0.9, use_sde=True, sde_sample_freq=4, learning_rate=1e-3, clip_range=0.4, policy_kwargs=policy_kwargs, verbose=3, tensorboard_log="./RL/panda_PPO_tensorboard/").learn(int(1e15), callback=callback, tb_log_name="telemetry")    
    
    model.save(os.path.join(log_dir, "final_model")) 
    
    stats_path = os.path.join(log_dir, "vec_normalize_final.pkl")
    env.save(stats_path)


