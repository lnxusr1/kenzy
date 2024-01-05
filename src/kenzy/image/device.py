# import sys
# import traceback
import os
import cv2
import threading
import queue
import time
from datetime import datetime
import logging
import copy
import collections
import math
import sys
import traceback
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.image.core import image_blur, image_gray, image_rotate, image_resize, \
    object_model, object_labels, get_face_encoding, \
    motion_detection, object_detection, face_detection
import kenzy.settings
from kenzy.extras import get_status
# from kenzy.image import core


class VideoProcessor:
    type = "kenzy.image"
    logger = logging.getLogger("KNZY-IMG")
    
    def __init__(self, **kwargs):
        self.settings = kwargs

        self.face_encodings = None
        self.face_names = None
        self.raw_width = None
        self.raw_height = None
        self.main_thread = None
        self.stop_event = threading.Event()
        self.record_event = threading.Event()
        self.restart_enabled = False

        self.obj_thread = None
        self.face_thread = None
        self.rec_thread = None
        self.callback_thread = None

        self.obj_queue = None
        self.face_queue = None
        self.rec_queue = None
        self.callback_queue = None

        self.location = kwargs.get("location", "Kenzy's Room")
        self.group = kwargs.get("group", "Kenzy's Group")
        self.service = None

        self.video_device = kwargs.get("video_device", 0)
        self.scale_factor = kwargs.get("scale", 1.0)
        self.frames_per_second = kwargs.get("frames_per_second")

        self.orientation = kwargs.get("orientation", 0)

        self.motion_enabled = kwargs.get("motion.detection", True)
        self.motion_threshold = kwargs.get("motion.threshold", 20)
        self.motion_area = kwargs.get("motion.area", 0.0003)

        self.object_detection = kwargs.get("object.detection", True)
        self.object_threshold = kwargs.get("object.threshold", 0.6)
        self.object_model_type = kwargs.get("object.model_type", "ssd")
        self.object_model_config = kwargs.get("object.model_config")
        self.object_model_file = kwargs.get("object.model_file")
        self.object_label_file = kwargs.get("objects.label_file")

        self.face_detection = kwargs.get("face.detection", True)
        self.face_recognition = kwargs.get("face.recognition", True)
        self.face_tolerance = kwargs.get("face.tolerance", 0.5)
        self.default_name = kwargs.get("face.default_name")
        self.cache_folder = kwargs.get("face.cache_folder")

        self.record_enabled = kwargs.get("record.enabled", True)
        self.video_format = kwargs.get("record.format", "XVID")
        self.video_folder = kwargs.get("record.folder")
        self.record_buffer = kwargs.get("record.buffer", 5)
        
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

        if self.settings.get("face.entries") is not None:
            self.face_names = []
            self.face_encodings = []
            for face_name in self.settings.get("face.entries", {}):
                image_list = self.settings.get("face.entries", {}).get(face_name)
                if isinstance(image_list, list):
                    for img in image_list:
                        self.face_encodings.append(get_face_encoding(img))    
                        self.face_names.append(face_name)
                else:
                    self.face_encodings.append(get_face_encoding(image_list))
                    self.face_names.append(face_name)

        if self.cache_folder is not None:
            cache_folder = os.path.expanduser(self.cache_folder)
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

        self.logger.debug("Starting object and motion detection thread")

        frames_per_second = self.frames_per_second

        model_labels = object_labels(label_file=self.object_label_file, model_type=self.object_model_type)
        model = object_model(model_type=self.object_model_type, model_config=self.object_model_config, model_file=self.object_model_file)

        last_person_seen = 0

        self.logger.debug("Object and motion detection thread started")

        while True:
            data = self.obj_queue.get()
            if data is None or not isinstance(data, dict):
                break

            if skip > 0:
                skip = skip - 1
                continue

            start = time.time()

            movements = None
            objects = None

            # Motion
            if self.motion_enabled:
                image = image_gray(data["frame"])
                image = image_blur(image)
                last_image_hold = copy.copy(image)
                movements = motion_detection(image=image, last_image=last_image, threshold=self.motion_threshold, motion_area=self.motion_area)
                last_image = last_image_hold

            # Object
            if self.object_detection:
                objects = object_detection(image=data["frame"], model=model, labels=model_labels, threshold=self.object_threshold)

            end = time.time()
            
            if (end - start) > 0:
                actual_fps = int(float(1 / (end - start)))  
                if actual_fps < frames_per_second:
                    if actual_fps > 0:
                        skip = math.ceil(frames_per_second / actual_fps) - 1

            ret = []
            if movements is not None:
                ret.extend(movements)

            if objects is not None:
                ret.extend(objects)

            curr_time = data.get("timestamp")

            rec_stop_time = self.recording_stop_time  # attempt to avoid segfault (should be atomic call)
            if objects is not None and "person" in [x.get("name") for x in objects]:
                if not self.record_event.is_set():
                    self.record_event.set()
                last_person_seen = curr_time
                self.recording_stop_time = 0
                rec_stop_time = 0

            elif rec_stop_time == 0 and self.record_event.is_set() and (last_person_seen + self.record_buffer) < curr_time:
                self.recording_stop_time = curr_time

            for item in ret:
                item["timestamp"] = curr_time

            self.callback_queue.put(ret)
            self.obj_queue.task_done()
        
    def _process_faces(self):
        skip = 0

        time.sleep(2)
        self.logger.debug("Starting face detection thread")

        frames_per_second = self.frames_per_second
        
        model_labels = object_labels(label_file=self.object_label_file, model_type=self.object_model_type)
        model = object_model(model_type=self.object_model_type, model_config=self.object_model_config, model_file=self.object_model_file)

        self.logger.debug("Face detection thread started")
        
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

            if hasFace and self.face_recognition:
                image = data["frame"]

                if width_calc > 1000:
                    image = image_resize(image, 0.2)
                elif width_calc > 600:
                    image = image_resize(image, 0.3)
                elif width_calc > 300:
                    image = image_resize(image, 0.5)
                elif width_calc > 200:
                    image = image_resize(image, 0.7)

                ret = face_detection(image=image, face_encodings=self.face_encodings, face_names=self.face_names, 
                                     tolerance=self.face_tolerance,
                                     default_name=self.default_name,
                                     cache_folder=self.cache_folder)
                faces.extend(ret)

                end = time.time()

                secs = (end - start)
                if secs == 0:
                    continue 

                if secs > 0:
                    actual_fps = float(1 / secs)
                    if actual_fps < frames_per_second:
                        if actual_fps > 0:
                            skip = math.ceil(float(frames_per_second) / actual_fps) - 1

                for item in faces:
                    item["timestamp"] = data.get("timestamp")

                self.callback_queue.put(faces)

            self.face_queue.task_done()

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
                    self.rec_queue.task_done()
                break

            if self.video_folder is not None and self.record_event.is_set():

                rec_stop_time = self.recording_stop_time  # Attempt to avoid segfault (should be atomic operation)
                if rec_stop_time != 0 and rec_stop_time <= data.get("timestamp"):
                    self.record_event.clear()
                    if video_writer is not None:
                        video_writer.release()
                        video_writer = None
                        self.record_event.clear()
                        self.rec_queue.task_done()
                    continue

                if video_writer is None:
                    ts = datetime.fromtimestamp(data.get("timestamp"))

                    file_name = os.path.join(
                        self.video_folder, 
                        ts.strftime("%Y%m%d"), 
                        ts.strftime("%Y%m%d_%H%M%S") + file_extension
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

            self.rec_queue.task_done()

    def _process_callback(self):
        motion = False
        last_motion = 0
        faces = {}

        last_motion_notify = False
        last_object_list = []

        while True:
            data = self.callback_queue.get()
            if data is None or not isinstance(data, list):
                break

            is_face_notice = False
            objects = []
            for item in data:
                if item.get("type") == "movement":
                    motion = True
                    last_motion = item.get("timestamp")
                elif item.get("type") == "object":
                    objects.append(item)    
                elif item.get("type") == "face":
                    faces[item.get("name", "Unknown")] = item
                    is_face_notice = True

            if motion and (time.time() - 1) > last_motion:
                motion = False

            if not is_face_notice:
                object_list = [x.get("name") for x in objects]
                object_list.sort()
                if motion != last_motion_notify or object_list != last_object_list:
                    self.service.collect(data={
                        "type": "kenzy.image",
                        "motion": motion,
                        "objects": objects,
                        "faces": faces
                    }, wait=False)

                    last_motion_notify = motion
                    last_object_list = object_list

            self.callback_queue.task_done()

    def _read_from_device(self):
        self.stop_event.clear()
        self.record_event.clear()
        read_counter = 0
        
        if self.frames_per_second is None:
            self.logger.critical("Invalid Frames Per Second.  Cancelling start")
            return
        
        dev = cv2.VideoCapture(self.video_device)

        try:
            while not self.stop_event.is_set():
                ret, frame = dev.read()

                if not ret:
                    read_counter += 1
                    if read_counter > 5:
                        raise Exception("Error, images failing reader.")
                else:
                    read_counter = 0
                    if self.scale_factor != 1.0:
                        frame = image_resize(frame, self.scale_factor)

                    frame = image_rotate(frame, self.orientation)

                    try:
                        if self.motion_enabled or self.object_detection:
                            self.obj_queue.put_nowait({ "frame": frame, "timestamp": time.time() })
                    except queue.Full:
                        # self.logger.debug("OBJECTS - Queue full.  Consider increasing frame_buffer_size.")
                        pass

                    try:
                        if self.face_detection:
                            self.face_queue.put_nowait({ "frame": frame, "timestamp": time.time() })
                    except queue.Full:
                        # self.logger.debug("FACES - Queue full.  Consider increasing frame_buffer_size.")
                        pass

                    try:
                        if self.record_enabled:
                            if self.record_event.is_set():
                                self.rec_queue.put_nowait({ "frame": frame, "timestamp": time.time() })
                            else:
                                self.frame_buffer.append(frame)
                    except queue.Full:
                        # self.logger.debug("RECORD - Queue full.  Consider increasing frame_buffer_size.")
                        pass

        except KeyboardInterrupt:
            self.stop()
        except Exception:
            self.logger.warning(f"Video read failed from {self.video_device}")
            self.logger.error(str(sys.exc_info()[0]))
            self.logger.error(str(traceback.format_exc()))
            self.logger.debug("Flagging for restart.")
            self.restart_enabled = True

        dev.release()

    @property
    def accepts(self):
        return ["start", "stop", "restart", "snapshot", "stream", "status", "get_settings", "set_settings"]

    def set_service(self, service):
        self.service = service

    def get_settings(self, **kwargs):
        return KenzyErrorResponse("Not Implemented")
    
    def set_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")

    def start(self, **kwargs):
        self.restart_enabled = False
        if self.is_alive():
            self.logger.error("Video Processor already running")
            return KenzyErrorResponse("Video Processor already running")

        # Insure we're good to start without already running routines       
        if (self.main_thread is not None and self.main_thread.is_alive()) \
                or (self.obj_thread is not None and self.obj_thread.is_alive()) \
                or (self.face_thread is not None and self.face_thread.is_alive()) \
                or (self.rec_thread is not None and self.rec_thread.is_alive()) \
                or (self.callback_thread is not None and self.callback_thread.is_alive()):

            self.stop()
        
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
        if (self.main_thread is None or not self.main_thread.is_alive()) \
                and (self.obj_thread is None or not self.obj_thread.is_alive()) \
                and (self.face_thread is None or not self.face_thread.is_alive()) \
                and (self.rec_thread is None or not self.rec_thread.is_alive()) \
                and (self.callback_thread is None or not self.callback_thread.is_alive()):

            self.logger.error("Video Processor is not running")
            return KenzyErrorResponse("Video Processor is not running")
        
        self.record_event.clear()
        self.stop_event.set()
        
        if self.main_thread.is_alive():
            self.main_thread.join()

        if self.obj_thread.is_alive():
            self.obj_queue.put(None)
            self.obj_thread.join()

        if self.face_thread.is_alive():
            self.face_queue.put(None)
            self.face_thread.join()

        if self.rec_thread.is_alive():
            self.rec_queue.put(None)
            self.rec_thread.join()

        if self.callback_thread.is_alive():
            self.callback_queue.put(None)
            self.callback_thread.join()

        if not self.is_alive():
            self.logger.info("Stopped Video Processor")
            return KenzySuccessResponse("Stopped Video Processor")
        else:
            self.logger.error("Unable to stop Video Processor")
            return KenzyErrorResponse("Unable to stop Video Processor")
    
    def restart(self, **kwargs):
        self.restart_enabled = False
        if (self.main_thread is not None and self.main_thread.is_alive()) \
                or (self.obj_thread is not None and self.obj_thread.is_alive()) \
                or (self.face_thread is not None and self.face_thread.is_alive()) \
                or (self.rec_thread is not None and self.rec_thread.is_alive()) \
                or (self.callback_thread is not None and self.callback_thread.is_alive()):
            
            ret = self.stop()
            if not ret.is_success():
                print(ret.get())
                return ret
        
        return self.start()

    def is_alive(self, **kwargs):
        if self.main_thread is not None and self.main_thread.is_alive():
            return True
        
        return False

    def snapshot(self, **kwargs):
        return KenzyErrorResponse("Not implemented")

    def status(self, **kwargs):
        return KenzySuccessResponse(get_status(self))
    
    def stream(self, **kwargs):
        return KenzyErrorResponse("Not implemented")