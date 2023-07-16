# import sys
# import traceback
import os
import cv2
import threading
import queue
import time
import logging
import collections
import math
from kenzy.core import KenzyResponse, KenzySuccessResponse, KenzyErrorResponse
# from kenzy.image import core


class VideoProcessor:
    type = "kenzy.image"
    location = None
    group = None
    detector = None
    service = None

    logger = logging.getLogger("KNZY-IMG")
    settings = {}
    video_device = 0
    read_thread = None
    proc_thread = None
    stop_event = threading.Event()
    frames = queue.Queue(20)
    frames_per_second = None
    image_decay = 1.0
    image_currency = 0.1
    image_check_frequency = 0.3
    video_folder = None 
    enable_recording = False
    object_list = []
    _use_objects = False
    video_format = "XVID"
    record_buffer = 5

    faces_detected = []
    objects_detected = []
    motion_detected = False
    faces_last = {}

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

        self.video_device = kwargs.get("video_device", 0)
        self.frames_per_second = kwargs.get("frames_per_second")
        self.image_decay = kwargs.get("image_decay", 1.0)
        self.image_currency = kwargs.get("image_currency", 0.1)
        self.image_check_frequency = kwargs.get("image_check_frequency", 0.3)
        self.video_folder = kwargs.get("video_folder", os.path.join(os.path.expanduser("~"), ".kenzy", "image", "videos"))
        self.enable_recording = kwargs.get("enable_recording", False)
        self.object_list = kwargs.get("object_list", [])
        self.video_format = kwargs.get("video_format", "XVID")
        self.record_buffer = kwargs.get("record_buffer", 5)

        self.validate_settings()

    def validate_settings(self):
        if self.video_folder is None: 
            if self.enable_recording:
                self.logger.info("Recording is disabled since video_folder was specified.")
                self.enable_recording = False
        else:
            self.video_folder = os.path.expanduser(self.video_folder)
            try:
                os.makedirs(self.video_folder, exist_ok=True)
            except Exception:
                self.logger.error("Recording is disabled since video_folder could not be created.")
                self.enable_recording = False

        if self.object_list is None or len(self.object_list) == 0:
            self._use_objects = False
        else:
            self._use_objects = True

        self.logger.info(f"video_folder:      {self.video_folder}")
        self.logger.info(f"enable_recording:  {self.enable_recording}")

    def _read_from_device(self):
        self.dev = cv2.VideoCapture(self.video_device)
        timestamp = 0
        try:
            while not self.stop_event.is_set():
                ret, frame = self.dev.read()
                if ret:
                    if self.frames_per_second is None:
                        if timestamp != 0:
                            self.frames_per_second = 1 / (time.time() - timestamp)

                        timestamp = time.time()

                    else:
                        timestamp = time.time()
                        try:
                            self.frames.put_nowait({ "frame": frame, "timestamp": timestamp })
                        except queue.Full:
                            pass

        except KeyboardInterrupt:
            self.stop()

        self.dev.release()

    def _process(self):
        time_lastface_check = 0
        frame_buffer = None

        do_record = False
        record_delay = 0

        video_writer = None
        fourcc = cv2.VideoWriter_fourcc(*self.video_format)

        file_extension = ".avi"
        if self.video_format.lower() == "mp4v":
            file_extension = ".m4v"
        elif self.video_format.lower() == "h264":
            file_extension = ".m4v"

        try:
            while not self.stop_event.is_set():
                try:
                    item = self.frames.get(timeout=0.1)
                    if item is None or not isinstance(item, dict) or item.get("timestamp") is None or item.get("frame") is None:
                        continue

                    # Prep for video recording
                    if self.frames_per_second is not None:
                        if frame_buffer is None:
                            frame_buffer = collections.deque(maxlen=int(math.ceil(self.frames_per_second)) * int(self.record_buffer))

                        if do_record or (record_delay + self.record_buffer) > item.get("timestamp"):
                            if video_writer is None:

                                file_name = os.path.join(
                                    self.video_folder, 
                                    time.strftime("%Y%m%d", time.gmtime(item.get("timestamp"))), 
                                    time.strftime("%Y%m%d_%H%M%S", time.gmtime(item.get("timestamp"))) + file_extension
                                )

                                try:
                                    os.makedirs(os.path.dirname(file_name), exist_ok=True)
                                except Exception:
                                    raise

                                video_writer = cv2.VideoWriter(
                                    os.path.join(file_name), 
                                    fourcc, 
                                    math.ceil(self.frames_per_second), 
                                    (item.get("frame").shape[1], item.get("frame").shape[0])
                                )

                            if frame_buffer is not None:
                                for frame in frame_buffer:
                                    video_writer.write(frame.get("frame"))

                                frame_buffer.clear()
                            
                            video_writer.write(item.get("frame"))
                        else:
                            if video_writer is not None:
                                video_writer.release()
                                frame_buffer.clear()
                                video_writer = None

                            frame_buffer.append(item)

                    # Main queue processor
                    current_time = time.time()

                    if item.get("timestamp") < current_time - self.image_currency:
                        continue  # Skip processing this frame as it is too old

                    detect_faces = True if self.detector._detectFaces and current_time - self.image_check_frequency < time_lastface_check else False
                    time_lastface_check = current_time

                    self.detector.analyze(item.get("frame"), detectFaces=detect_faces)
                    
                    motion_detected = False
                    if self.detector._detectMotion and len(self.detector.movements) > 0:
                        motion_detected = True

                    objects_detected = [x["name"] for x in self.detector.objects]
                    objects_detected.sort()

                    if self.enable_recording and self._use_objects:
                        for ob in self.object_list:
                            if ob in objects_detected:
                                do_record = True
                                record_delay = item.get("timestamp")

                        do_record = False

                    if detect_faces:
                        faces_detected = [x.get("name", self.detector._defaultFaceName) for x in self.detector.faces if x.get("name") is not None]
                        faces_detected.sort()
                    else:
                        faces_detected = [x["name"] for x in self.faces_detected]
                    
                    od = [x["name"] for x in self.objects_detected]
                    od.sort()

                    fd = [x["name"] for x in self.faces_detected]
                    fd.sort()

                    bChanged = False
                    if motion_detected != self.motion_detected:
                        bChanged = True
                        self.motion_detected = motion_detected

                    if objects_detected != od:
                        bChanged = True
                        self.objects_detected = [{"name": x, "timestamp": item.get("timestamp") } for x in objects_detected]

                    if faces_detected != fd:
                        bChanged = True
                        # Clear out old names or unidentified faces
                        for i in range(len(self.faces_detected), 0, -1):
                            if self.faces_detected[i - 1]["name"] == self.detector._defaultFaceName \
                                    or self.faces_detected[i - 1]["timestamp"] < item.get("timestamp") - self.image_decay:
                                
                                del self.faces_detected[i - 1]

                        # Add in new names
                        t_arr = []
                        for faceName in faces_detected:
                            bFound = False
                            for face in self.faces_detected:
                                if faceName != self.detector._defaultFaceName and faceName == face.get("name"):
                                    bFound = True
                                    self.faces_last[str(face.get("name"))] = item.get("timestamp")

                            if not bFound and faceName is not None:
                                t_arr.append({ "name": faceName, "timestamp": item.get("timestamp") })
                        errors=None
                        self.faces_detected.extend(t_arr)

                    if bChanged:
                        self.service.collect({ 
                            "data": {
                                "motion": motion_detected,
                                "objects": objects_detected,
                                "faces": self.faces_detected
                            },
                            "type": self.type
                        })

                        # print(motion_detected, objects_detected, self.faces_detected)

                except queue.Empty:
                    pass

        except KeyboardInterrupt:
            self.stop()

    @property
    def accepts(self):
        return ["start", "stop", "restart", "snapshot", "stream", "is_alive"]

    def set_component(self, component):
        self.detector = component

        self.detector._recognizeFaces = True

    def set_service(self, service):
        self.service = service

    def start(self, **kwargs):
        if self.detector is None:
            self.logger.error("Detector not set.  Start request failed.")
            return KenzyErrorResponse("Detector not set.  Start request failed.")
        
        if self.read_thread is not None or self.proc_thread is not None:
            self.logger.error("Unable to start Video Processor.  Threads already exist.")
            return KenzyErrorResponse("Unable to start Video Processor.  Threads already exist.")
        
        self.read_thread = threading.Thread(target=self._read_from_device)
        self.read_thread.daemon = True
        self.read_thread.start()

        self.proc_thread = threading.Thread(target=self._process)
        self.proc_thread.daemon = True
        self.proc_thread.start()

        self.logger.info("Started Video Processor")
        return KenzySuccessResponse("Started Video Processor")

    def stop(self, **kwargs):
        self.stop_event.set()

        if self.read_thread is not None and self.read_thread.is_alive():
            self.read_thread.join()

        if self.proc_thread is not None and self.proc_thread.is_alive():
            self.proc_thread.join()

        self.read_thread = None
        self.proc_thread = None
        self.stop_event.clear()

        self.logger.info("Stopped Video Processor")
        return KenzySuccessResponse("Stopped Video Processor")
    
    def restart(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            ret = self.stop()
            if not ret.is_success():
                return ret
        
        return self.start()

    def is_alive(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            return True
        
        return False

    def snapshot(self, **kwargs):
        raise NotImplementedError("Feature not implemented.")

    def stream(self, **kwargs):
        raise NotImplementedError("Feature not implemented.")