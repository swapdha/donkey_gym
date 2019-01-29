'''
file: donkey_sim.py
author: Tawn Kramer
date: 2018-08-31
'''

import os
import json
import shutil
import base64
import random
import time
from io import BytesIO
import math
from threading import Thread

import numpy as np
from PIL import Image
from io import BytesIO
import base64
import datetime
import asyncore

from donkey_gym.core.fps import FPSTimer
from donkey_gym.core.tcp_server import IMesgHandler, SimServer
from donkey_gym.envs.donkey_ex import SimFailed

class DonkeyUnitySimContoller():

    def __init__(self, level, time_step=0.05, port=9090, max_cte=5.0, verbose=False, cam_resolution=(120, 160, 3)):
        self.address = ('0.0.0.0', port)

        self.handler = DonkeyUnitySimHandler(level, time_step=time_step, max_cte=max_cte, verbose=verbose, cam_resolution=cam_resolution)

        try:
            self.server = SimServer(self.address, self.handler)
        except OSError:
            print('raising custom error')
            raise SimFailed("failed to listen on address %s" % self.address)

        self.thread = Thread(target=asyncore.loop)
        self.thread.daemon = True
        self.thread.start()

    def wait_until_loaded(self):
        while not self.handler.loaded:
            print("waiting for sim to start..")
            time.sleep(3.0)

    def reset(self):
        self.handler.reset()

    def get_sensor_size(self):
        return self.handler.get_sensor_size()

    def take_action(self, action):
        self.handler.take_action(action)

    def observe(self):
        return self.handler.observe()

    def quit(self):
        pass

    def render(self, mode):
        pass

    def is_game_over(self):
        return self.handler.is_game_over()

    def calc_reward(self, done):
        return self.handler.calc_reward(done)


class DonkeyUnitySimHandler(IMesgHandler):

    def __init__(self, level, time_step=0.05, max_cte=5.0, verbose=False, cam_resolution=None):
        self.iSceneToLoad = level
        self.time_step = time_step
        self.wait_time_for_obs = 0.1
        self.sock = None
        self.loaded = False
        self.verbose = verbose
        self.max_cte = max_cte
        self.timer = FPSTimer()

        # sensor size - height, width, depth
        self.camera_img_size = cam_resolution
        self.image_array = np.zeros(self.camera_img_size)
        self.last_obs = None
        self.hit = "none"
        self.cte = 0.0
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.speed = 0.0
        self.over = False
        self.fns = {'telemetry' : self.on_telemetry,
                    "scene_selection_ready" : self.on_scene_selection_ready,
                    "scene_names": self.on_recv_scene_names,
                    "car_loaded" : self.on_car_loaded }

    def on_connect(self, socketHandler):
        self.sock = socketHandler

    def on_disconnect(self):
        self.sock = None

    def on_recv_message(self, message):
        if not 'msg_type' in message:
            print('expected msg_type field')
            return

        msg_type = message['msg_type']
        if msg_type in self.fns:
            self.fns[msg_type](message)
        else:
            print('unknown message type', msg_type)

    ## ------- Env interface ---------- ##

    def reset(self):
        if self.verbose:
            print("reseting")
        self.image_array = np.zeros(self.camera_img_size)
        self.last_obs = self.image_array
        self.hit = "none"
        self.cte = 0.0
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.speed = 0.0
        self.over = False
        self.send_reset_car()
        self.timer.reset()
        time.sleep(1)

    def get_sensor_size(self):
        return self.camera_img_size

    def take_action(self, action):
        if self.verbose:
            print("take_action")

        self.send_control(action[0], action[1])

    def observe(self):
        while self.last_obs is self.image_array:
            time.sleep(1.0 / 120.0)

        self.last_obs = self.image_array
        observation = self.image_array
        done = self.is_game_over()
        reward = self.calc_reward(done)
        info = {}

        self.timer.on_frame()

        return observation, reward, done, info


    def is_game_over(self):
        return self.over

    ## ------ RL interface ----------- ##

    def calc_reward(self, done):
        if done:
            return -1.0

        if self.cte > self.max_cte:
            return -1.0

        if self.hit != "none":
            return -2.0

        #going fast close to the center of lane yeilds best reward
        return 1.0 - (self.cte / self.max_cte) * self.speed


    ## ------ Socket interface ----------- ##

    def on_telemetry(self, data):

        imgString = data["image"]
        image = Image.open(BytesIO(base64.b64decode(imgString)))

        #always update the image_array as the observation loop will hang if not changing.
        self.image_array = np.asarray(image)

        #don't update other telemetry once session over
        if self.over:
            return

        self.hit = data["hit"]
        self.x = data["pos_x"]
        self.y = data["pos_y"]
        self.z = data["pos_z"]
        self.speed = data["speed"]

        #Cross track error not always present.
        #Will be missing if path is not setup in the given scene.
        #It should be setup in the 4 scenes available now.
        try:
            self.cte = data["cte"]
        except:
            pass

        self.determine_episode_over()


    def determine_episode_over(self):
        #we have a few initial frames on start that are sometimes very large CTE when it's behind
        #the path just slightly. We ignore those.
        if math.fabs(self.cte) > 2 * self.max_cte:
            pass
        elif math.fabs(self.cte) > self.max_cte:
            if self.verbose:
                print("game over: cte", self.cte)
            self.over = True
        elif self.hit != "none":
            if self.verbose:
                print("game over: hit", self.hit)
            self.over = True


    def on_scene_selection_ready(self, data):
        print("SceneSelectionReady ")
        self.send_get_scene_names()

    def on_car_loaded(self, data):
        if self.verbose:
            print("car loaded")
        self.loaded = True

    def on_recv_scene_names(self, data):
        if data:
            names = data['scene_names']
            if self.verbose:
                print("SceneNames:", names)
            self.send_load_scene(names[self.iSceneToLoad])

    def send_control(self, steer, throttle):
        if not self.loaded:
            return
        msg = { 'msg_type' : 'control', 'steering': steer.__str__(), 'throttle':throttle.__str__(), 'brake': '0.0' }
        self.queue_message(msg)

    def send_reset_car(self):
        msg = { 'msg_type' : 'reset_car' }
        self.queue_message(msg)

    def send_get_scene_names(self):
        msg = { 'msg_type' : 'get_scene_names' }
        self.queue_message(msg)

    def send_load_scene(self, scene_name):
        msg = { 'msg_type' : 'load_scene', 'scene_name' : scene_name }
        self.queue_message(msg)

    def queue_message(self, msg):
        if self.sock is None:
            if self.verbose:
                print('skiping:', msg)
            return

        if self.verbose:
            print('sending', msg)
        self.sock.queue_message(msg)
