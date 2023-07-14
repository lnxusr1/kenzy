import sys
import traceback
import cv2
import threading
import queue
import time
import logging
import collections
# from kenzy.image import core


class VideoProcessor:
    type = "kenzy.image"
    logger = logging.getLogger("KNZY-IMG")
    settings = {}
    video_device = 0
    detector = None
    service = None
    read_thread = None
    proc_thread = None
    stop_event = threading.Event()
    frames = queue.Queue(20)
    frames_per_second = None

    faces_detected = []
    objects_detected = []
    motion_detected = False

    def __init__(self, **kwargs):
        self.settings = kwargs
        self.video_device = kwargs.get("video_device", 0)
        self.frames_per_second = kwargs.get("frames_per_second")

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
        
        if self.frames_per_second is None:
            frame_buffer = None
        else:
            frame_buffer = collections.dequeue(maxlen=int(self.frames_per_second) * 5)

        # TODO: setup video recording

        try:
            while not self.stop_event.is_set():
                try:
                    item = self.frames.get(timeout=0.1)
                    if item is None or not isinstance(item, dict) or item.get("timestamp") is None or item.get("frame") is None:
                        continue
                    
                    current_time = time.time()

                    if item.get("timestamp") < current_time - .1:
                        continue  # Skip processing this frame as it is too old

                    detect_faces = True if self.detector._detectFaces and current_time - 0.3 < time_lastface_check else False
                    time_lastface_check = current_time

                    self.detector.analyze(item.get("frame"), detectFaces=detect_faces)
                    
                    motion_detected = False
                    if self.detector._detectMotion and len(self.detector.movements) > 0:
                        motion_detected = True

                    objects_detected = [x["name"] for x in self.detector.objects]
                    objects_detected.sort()

                    if detect_faces:
                        faces_detected = [x.get("name", self.detector._defaultFaceName) for x in self.detector.faces]
                        faces_detected.sort()
                    else:
                        faces_detected = self.faces_detected
                    
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
                                    or self.faces_detected[i - 1]["timestamp"] < item.get("timestamp") - 1:
                                
                                del self.faces_detected[i - 1]

                        # Add in new names
                        for faceName in faces_detected:
                            bFound = False
                            for face in self.faces_detected:
                                if faceName != self.detector._defaultFaceName and faceName == face.get("name"):
                                    bFound = True

                            if not bFound and faceName:
                                self.faces_detected.append({ "name": faceName, "timestamp": item.get("timestamp") })
                        
                        #self.faces_detected = [{"name": x, "timestamp": item.get("timestamp") } for x in faces_detected]

                    if bChanged:
                        print(motion_detected, objects_detected, self.faces_detected)

                    #print(True if len(self.detector.movements) > 0 else False, len(self.detector.objects), len(self.detector.faces))

                except queue.Empty:
                    pass

        except KeyboardInterrupt:
            self.stop()

    @property
    def accepts(self):
        return ["start", "stop", "restart", "snapshot", "stream", "is_alive"]

    def set_component(self, component):
        self.detector = component

    def set_service(self, service):
        self.service = service

    def start(self, **kwargs):
        if self.detector is None:
            self.logger.error("Detector not set.  Start request failed.")
            return False
        
        if self.read_thread is not None or self.proc_thread is not None:
            self.logger.error("Unable to start Video Processor.  Threads already exist.")
            return False
        
        self.read_thread = threading.Thread(target=self._read_from_device)
        self.read_thread.daemon = True
        self.read_thread.start()

        self.proc_thread = threading.Thread(target=self._process)
        self.proc_thread.daemon = True
        self.proc_thread.start()

        self.logger.info("Started Video Processor")

        return True

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
        return True
    
    def restart(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            if not self.stop():
                return False
        
        return self.start()

    def is_alive(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            return True
        
        return False

    def snapshot(self, **kwargs):
        raise NotImplementedError("Feature not implemented.")

    def stream(self, **kwargs):
        raise NotImplementedError("Feature not implemented.")