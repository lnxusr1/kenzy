# import sys
# import traceback
import os
import cv2
import threading
import queue
import time
import logging
import copy
import collections
import math
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.image.core import image_blur, image_gray, rotate_image, resize_image, \
    object_model, object_labels, get_face_encoding, \
    motion_detection, object_detection, face_detection
import kenzy.settings
# from kenzy.image import core


class VideoProcessor:
    type = "kenzy.image"

    location = None
    group = None
    service = None
    orientation = 0
    video_device = 0
    frames_per_second = None
    record_buffer = 5
    motion_threshold = 20
    motion_area = 0.0003
    object_threshold = 0.5
    face_tolerance = 0.6
    video_format = "XVID"
    video_folder = None

    logger = logging.getLogger("KNZY-IMG")
    settings = {}
    face_encodings = None
    face_names = None
    raw_width = None
    raw_height = None
    main_thread = None
    stop_event = threading.Event()
    record_event = threading.Event()

    obj_thread = None
    face_thread = None
    rec_thread = None
    callback_thread = None

    obj_queue = None
    face_queue = None
    rec_queue = None
    callback_queue = None

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

        self.video_device = kwargs.get("video_device", 0)
        self.frames_per_second = kwargs.get("frames_per_second")

        self.orientation = kwargs.get("orientation", 0)

        self.motion_threshold = kwargs.get("motion_threshold", 20)
        self.motion_area = kwargs.get("motion_area", 0.0003)
        self.object_threshold = kwargs.get("object_threshold", 0.6)

        self.face_tolerance = kwargs.get("face_tolerance", 0.5)

        self.video_format = kwargs.get("video_format", "XVID")
        self.video_folder = kwargs.get("video_folder")
        self.record_buffer = kwargs.get("record_buffer", 5)

        self.initialize_settings()

    def initialize_settings(self):
        self.face_encodings = None
        self.face_names = None

        dev = cv2.VideoCapture(self.video_device)

        if self.frames_per_second is None:
            (major_ver, minor_ver, subminor_ver) = (cv2.__version__).split('.')
            if int(major_ver) < 3:
                self.frames_per_second = dev.get(cv2.cv.CV_CAP_PROP_FPS)
                self.logger.debug("Setting frame rate: {0}".format(self.frames_per_second))
            else:
                self.frames_per_second = dev.get(cv2.CAP_PROP_FPS)
                self.logger.debug("Setting frame rate: {0}".format(self.frames_per_second))

        ret, frame = dev.read()
        dev.release()

        if ret:
            self.raw_width = frame.shape[1]
            self.raw_height = frame.shape[0]

        if self.settings.get("faces") is not None:
            self.face_names = []
            self.face_encodings = []
            for face_name in self.settings.get("faces", {}):
                image_list = self.settings.get("faces", {}).get(face_name)
                if isinstance(image_list, list):
                    for img in image_list:
                        self.face_encodings.append(get_face_encoding(img))    
                        self.face_names.append(face_name)
                else:
                    self.face_encodings.append(get_face_encoding(image_list))
                    self.face_names.append(face_name)

        if self.settings.get("cache_folder") is not None:
            cache_folder = os.path.expanduser(self.settings.get("cache_folder"))
            if os.path.isfile(os.path.join(cache_folder, "cache.yml")):
                
                if self.face_names is None:
                    self.face_names = []
                
                if self.face_encodings is None:
                    self.face_encodings = []

                data = kenzy.settings.load(os.path.join(cache_folder, "cache.yml"))
                for face_name in data:
                    try:
                        img = os.path.join(cache_folder, data[face_name])
                        self.face_encodings.append(get_face_encoding(img))    
                        self.face_names.append(face_name)
                    except Exception:
                        pass

        if self.video_folder is not None:
            self.video_folder = os.path.expanduser(self.video_folder)

        self.frame_buffer = collections.deque(maxlen=int(self.frames_per_second * self.record_buffer))
        self.recording_stop_time = 0

    def _process_motion_and_objects(self):
        last_image = None
        skip = 0

        model_labels = object_labels()
        model = object_model()

        last_person_seen = 0

        while True:
            data = self.obj_queue.get()
            if data is None or not isinstance(data, dict):
                break

            if skip > 0:
                skip = skip - 1
                continue

            start = time.time()

            # Motion
            image = image_gray(data["frame"])
            image = image_blur(image)
            last_image_hold = copy.copy(image)
            movements = motion_detection(image=image, last_image=last_image, threshold=self.motion_threshold, motion_area=self.motion_area)
            last_image = last_image_hold

            # Object
            objects = object_detection(image=data["frame"], model=model, labels=model_labels, threshold=self.object_threshold)

            end = time.time()
            
            actual_fps = int(float(1 / (end - start)))  
            if actual_fps < self.frames_per_second:
                skip = math.ceil(self.frames_per_second / actual_fps) - 1

            ret = []
            if movements is not None:
                ret.extend(movements)

            if objects is not None:
                ret.extend(objects)

            curr_time = data.get("timestamp")

            rec_stop_time = self.recording_stop_time  # attempt to avoid segfault (should be atomic call)
            if "person" in [x.get("name") for x in objects]:
                if not self.record_event.is_set():
                    self.record_event.set()
                last_person_seen = curr_time
                self.recording_stop_time = 0

            elif rec_stop_time == 0 and self.record_event.is_set() and (last_person_seen + self.record_buffer) < curr_time:
                self.recording_stop_time = curr_time

            for item in ret:
                item["timestamp"] = curr_time

            self.callback_queue.put(ret)
        
    def _process_faces(self):
        skip = 0

        model_labels = object_labels()
        model = object_model()
        
        while True:
            data = self.face_queue.get()
            if data is None or not isinstance(data, dict):
                break

            if skip > 0:
                skip = skip - 1
                continue

            start = time.time()

            faces = []
            hasFace = False
            width_calc = 100000
            objects = object_detection(image=data["frame"], model=model, labels=model_labels, threshold=self.object_threshold)
            if objects is not None:
                for item in objects:
                    if item["name"] == "person":
                        hasFace = True
                        
                        loc = item["location"]
                        left = loc["left"]
                        right = loc["right"]
                        new_calc = right - left
                        if new_calc < width_calc:
                            width_calc = new_calc

            if hasFace:
                image = data["frame"]

                if width_calc > 1000:
                    image = resize_image(image, 0.2)
                elif width_calc > 600:
                    image = resize_image(image, 0.3)
                elif width_calc > 300:
                    image = resize_image(image, 0.5)
                elif width_calc > 200:
                    image = resize_image(image, 0.7)

                ret = face_detection(image=image, face_encodings=self.face_encodings, face_names=self.face_names, 
                                     tolerance=self.face_tolerance,
                                     default_name=self.settings.get("default_name"),
                                     cache_folder=self.settings.get("cache_folder"))
                faces.extend(ret)

                end = time.time()

                secs = (end - start)
                if secs == 0:
                    continue 

                actual_fps = float(1 / secs)
                if actual_fps < self.frames_per_second:
                    skip = math.ceil(float(self.frames_per_second) / actual_fps) - 1

                for item in faces:
                    item["timestamp"] = data.get("timestamp")

                self.callback_queue.put(faces)

    def _process_record(self):

        video_writer = None
        fourcc = cv2.VideoWriter_fourcc(*self.video_format)

        file_extension = ".avi"
        if self.video_format.lower() == "mp4v":
            file_extension = ".m4v"
        elif self.video_format.lower() == "h264":
            file_extension = ".m4v"

        while True:
            data = self.rec_queue.get()
            if data is None or not isinstance(data, dict):
                if video_writer is not None:
                    video_writer.release()
                    video_writer = None
                    self.record_event.clear()
                break

            if self.video_folder is not None and self.record_event.is_set():

                rec_stop_time = self.recording_stop_time  # Attempt to avoid segfault (should be atomic operation)
                if rec_stop_time != 0 and rec_stop_time <= data.get("timestamp"):
                    self.record_event.clear()

                if video_writer is None:
                    file_name = os.path.join(
                        self.video_folder, 
                        time.strftime("%Y%m%d", time.gmtime(data.get("timestamp"))), 
                        time.strftime("%Y%m%d_%H%M%S", time.gmtime(data.get("timestamp"))) + file_extension
                    )
                    self.logger.debug(f"Recording to {file_name}")

                    try:
                        os.makedirs(os.path.dirname(file_name), exist_ok=True)
                    except Exception:
                        raise

                    video_writer = cv2.VideoWriter(
                        os.path.join(file_name), 
                        fourcc, 
                        math.ceil(self.frames_per_second), 
                        (data.get("frame").shape[1], data.get("frame").shape[0])
                    )

                    if self.frame_buffer is not None:
                        for frame in self.frame_buffer:
                            video_writer.write(frame)

                        self.frame_buffer.clear()
                    
                video_writer.write(data.get("frame"))

            else:
                if video_writer is not None:
                    video_writer.release()
                    video_writer = None

    def _process_callback(self):
        motion = False
        last_motion = 0
        faces = {}

        while True:
            data = self.callback_queue.get()
            if data is None or not isinstance(data, list):
                break

            objects = []
            for item in data:
                if item.get("type") == "movement":
                    motion = True
                    last_motion = item.get("timestamp")
                elif item.get("type") == "object":
                    objects.append(item)    
                elif item.get("type") == "face":
                    faces[item.get("name", "Unknown")] = item

            if motion and (time.time() - 1) > last_motion:
                motion = False

            self.service.collect(data={
                "motion": motion,
                "objects": objects,
                "faces": faces
            })

    def _read_from_device(self):
        self.stop_event.clear()
        self.record_event.clear()
        
        if self.frames_per_second is None:
            self.logger.critical("Invalid Frames Per Second.  Cancelling start")
            return
        
        dev = cv2.VideoCapture(self.video_device)

        try:
            while not self.stop_event.is_set():
                ret, frame = dev.read()

                if ret:
                    frame = rotate_image(frame, self.orientation)
                    try:
                        self.obj_queue.put_nowait({ "frame": frame, "timestamp": time.time() })
                    except queue.Full:
                        # self.logger.debug("OBJECTS - Queue full.  Consider increasing frame_buffer_size.")
                        pass

                    try:
                        self.face_queue.put_nowait({ "frame": frame, "timestamp": time.time() })
                    except queue.Full:
                        # self.logger.debug("FACES - Queue full.  Consider increasing frame_buffer_size.")
                        pass

                    try:
                        if self.record_event.is_set():
                            self.rec_queue.put_nowait({ "frame": frame, "timestamp": time.time() })
                        else:
                            self.frame_buffer.append(frame)
                    except queue.Full:
                        self.logger.debug("RECORD - Queue full.  Consider increasing frame_buffer_size.")
                        pass

        except KeyboardInterrupt:
            self.stop()

        dev.release()

    @property
    def accepts(self):
        return ["start", "stop", "restart", "snapshot", "stream", "status", "get_settings", "set_settings"]

    def set_component_settings(self, **kwargs):
        if kwargs is None or not isinstance(kwargs, dict):
            kwargs = {}

        if self.motion_area is not None and self.raw_height is not None and self.raw_width is not None:
            kwargs["motionMinArea"] = int((self.raw_width * self.raw_height) * self.motion_area)

        self.component_settings = kwargs

    def set_service(self, service):
        self.service = service

    def set_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")

    def start(self, **kwargs):

        if self.is_alive():
            self.logger.error("Video Processor already running")
            return KenzyErrorResponse("Video Processor already running")
        
        self.frame_buffer.clear()
        
        self.obj_queue = queue.Queue(1)  # int(self.frame_buffer_size * self.frames_per_second))
        self.obj_thread = threading.Thread(target=self._process_motion_and_objects, daemon=True)
        self.obj_thread.start()

        self.face_queue = queue.Queue(1)  # int(self.frame_buffer_size * self.frames_per_second))
        self.face_thread = threading.Thread(target=self._process_faces, daemon=True)
        self.face_thread.start()

        self.rec_queue = queue.Queue()  # int(self.frame_buffer_size * self.frames_per_second))
        self.rec_thread = threading.Thread(target=self._process_record, daemon=True)
        self.rec_thread.start()

        self.callback_queue = queue.Queue()
        self.callback_thread = threading.Thread(target=self._process_callback, daemon=True)
        self.callback_thread.start()

        self.main_thread = threading.Thread(target=self._read_from_device, daemon=True)
        self.main_thread.start()

        if self.is_alive():
            self.logger.info("Started Video Processor")
            return KenzySuccessResponse("Started Video Processor")
        else:
            self.logger.error("Unable to start Video Processor")
            return KenzyErrorResponse("Unable to start Video Processor")

    def stop(self, **kwargs):
        if self.main_thread is None or not self.main_thread.is_alive():
            self.logger.error("Video Processor is not running")
            return KenzyErrorResponse("Video Processor is not running")
        
        self.record_event.clear()
        self.stop_event.set()
        self.main_thread.join()

        self.obj_queue.put(None)
        self.obj_thread.join()

        self.face_queue.put(None)
        self.face_thread.join()

        self.rec_queue.put(None)
        self.rec_thread.join()

        self.callback_queue.put(None)
        self.callback_thread.join()

        if not self.is_alive():
            self.logger.info("Stopped Video Processor")
            return KenzySuccessResponse("Stopped Video Processor")
        else:
            self.logger.error("Unable to stop Video Processor")
            return KenzyErrorResponse("Unable to stop Video Processor")
    
    def restart(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            ret = self.stop()
            if not ret.is_success():
                return ret
        
        return self.start()

    def is_alive(self, **kwargs):
        if self.main_thread is not None and self.main_thread.is_alive():
            return True
        
        return False

    def snapshot(self, **kwargs):
        return KenzyErrorResponse("Not implemented")

    def status(self, **kwargs):
        return KenzySuccessResponse({
            "active": self.is_alive(),
            "type": self.type,
            "accepts": self.accepts,
            "data": {
            }
        })
    
    def stream(self, **kwargs):
        return KenzyErrorResponse("Not implemented")