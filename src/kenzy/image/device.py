import cv2
import threading
import queue
import time
import logging
from kenzy.image.core import detector


class VideoDevice:
    logger = logging.getLogger("IMAGE-DEV")
    settings = {}
    video_device = 0
    faces_folder = None
    detector = None
    server = None
    read_thread = None
    proc_thread = None
    stop_event = threading.Event()
    frames = queue.Queue(20)

    def __init__(self, server=None, **kwargs):
        self.server = server
        self.settings = kwargs

        self.video_device = kwargs.get("video_device", 0)
        self.detector = detector(**kwargs)

        # TODO: Process Faces Folder
        self.faces_folder = kwargs.get("faces_folder")

    def _read_from_device(self):
        self.dev = cv2.VideoCapture(self.video_device)
        
        try:
            while not self.stop_event.isSet():
                ret, frame = self.dev.read()
                if ret:
                    timestamp = time.time()
                    try:
                        self.frames.put_nowait({ "frame": frame, "timestamp": timestamp })
                    except queue.Full:
                        pass

        except KeyboardInterrupt:
            self.stop()

        self.dev.release()

    def _process(self):
        try:
            while not self.stop_event.isSet():
                try:
                    item = self.frames.get(timeout=0.1)
                    if item is None:
                        continue 
                    
                    if item.get("timestamp") < time.time() - .1:
                        continue  # Skip processing this frame as it is too old

                    # TODO: Analyze Images, Add snaps, Record if needed, Make callback
                    self.detector.analyze(item.get("frame"))
                    print(True if len(self.detector.movements) > 0 else False, len(self.detector.objects), len(self.detector.faces))

                except queue.Empty:
                    pass

        except KeyboardInterrupt:
            self.stop()

    @property
    def accepts(self):
        return ["start", "stop", "restart", "snapshot", "stream", "is_alive"]

    def start(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            raise Exception("Unable to start.  Thread already exists.")
        
        self.read_thread = threading.Thread(target=self._read_from_device)
        self.read_thread.daemon = True
        self.read_thread.start()

        self.proc_thread = threading.Thread(target=self._process)
        self.proc_thread.daemon = True
        self.proc_thread.start()

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

        return True
    
    def restart(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            if not self.stop():
                return False
        
        return self.start()
    
    def is_alive(self, **kwargs):
        if self.stop_event.isSet():
            return True
        
        return False

    def snapshot(self, **kwargs):
        raise NotImplementedError("Feature not implemented.")

    def stream(self, **kwargs):
        raise NotImplementedError("Feature not implemented.")