import multiprocessing as mp
import os
import logging
import time
from kenzy.shared import threaded, pthreaded, TCPStreamingClient
from kenzy import GenericDevice
import cv2 
import sys
import traceback
import copy
import collections


try:
    from kenzy_image.core import detector
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Detector failed to initialize.")


class Watcher(GenericDevice):
    """
    Watcher device to capture and process inbound video stream for objects and faces.
    """
    
    def __init__(self, callback=None, parent=None, **kwargs):
        """
        Watcher Initialization

        Args:
            classifierFile (str):  Classifier file such as haarcascades to identify generic objects.
            recognizerFile (str): Trained file to be used to identify specific objects. (optional)
            namesFile (str):  File with friendly names tied to recognizer trained data set. (optional)
            trainingSourceFolder (str):  The source directory that contains all the images to use for building a new recognizerFile.
            framesPerSecond (float): Number of frames per second.  Defaults to NTSC.
            orientation (int): Device orientation which can be 0, 90, 180, or 270.  (optional)
            videoDeviceIndex (int): Video device index number.  If not set then will use default video capture device.

            callback (function): Callback function for which to send any captured data.
            parent (object): Containing object's reference.  Normally this would be the device container. (optional)
        """

        # Local variable instantiation and initialization
        self.type = "WATCHER"
        self.logger = logging.getLogger(kwargs.get("nickname", self.type))

        self.clients = []
        self._isRunning = False
        self.lastFrame = None

        from kenzy import __version__
        self.version = __version__
        self._packageName = "kenzy"

        super(Watcher, self).__init__(callback=callback, parent=parent, **kwargs)
        
    def updateSettings(self):

        self.parent = self.args.get("parent")
        self._callbackHandler = self.args.get("callback")                       # Callback function accepts two positional args (Type, Text)
                
        self.videoDeviceIndex = self.args.get("videoDeviceIndex", 0)
        
        self.orientation = None
        orientation = self.args.get("orientation", 0)
        if orientation == 90:
            self.orientation = cv2.ROTATE_90_CLOCKWISE
        elif orientation == 180:
            self.orientation = cv2.ROTATE_180
        elif orientation == 270 or orientation == -90:
            self.orientation = cv2.ROTATE_90_COUNTERCLOCKWISE

        return True

    @threaded
    def _doCallback(self, inData):
        """
        Calls the specified callback as a thread to keep from blocking additional processing.

        Args:
            text (str):  Text to send to callback function
        
        Returns:
            (thread):  The thread on which the callback is created to be sent to avoid blocking calls.
        """

        try:
            if self.callback is not None:
                self.logger.debug(str(inData))
                self.callback("IMAGE_INPUT", inData)
        except Exception:
            pass
        
        return
    
    @threaded
    def _readFromCamera(self):
        """
        Opens video device for capture and processing for inputs
        
        Returns:
            (thread):  The thread created for the watcher while capturing incoming video.
        """
        
        enableDetection = True
                
        try:
            args = copy.copy(self.args)
            if "orientation" in args:
                del args["orientation"]
                
        except Exception:
            enableDetection = False
            self.logger.warning("Reverting to simple image capture device. (Did you install kenzy-image?)")

        videoDevice = cv2.VideoCapture(self.videoDeviceIndex)

        threadPool = []
        
        # Test camera to make sure we can read a frame
        ret, im = videoDevice.read()
        if not ret:
            self.logger.error("Unable to read from camera device. Is the device connected?")
            self._isRunning = False
            self.stop()
            return False

        self._isRunning = True
        if enableDetection:
            q_analyze = mp.Queue()
            mp_analyze_process = process(q_analyze, self.args, self._doCallback)

        while self._isRunning:
            ret, im = videoDevice.read()
            if ret:
                
                # See if we need to rotate it and do so if required
                if self.orientation is not None:
                    im = cv2.rotate(im, self.orientation)
                
                if enableDetection:
                    q_analyze.put({ "img": im, "time": time.time() })

                width = int(im.shape[1])
                height = int(im.shape[0])
                
                if width > 640:
                    width = 640
                    height = int(im.shape[0]) / int(im.shape[1]) * width
                    
                if height > 480:
                    height = 480
                    width = int(im.shape[1]) / int(im.shape[0]) * height
                
                dim = (int(width), int(height))
                im = cv2.resize(im, dim, interpolation=cv2.INTER_AREA)

                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 60]  # This can be increated to 100, but at the cost of bandwidth.
                data = cv2.imencode('.jpg', im, encode_param)[1]
                
                try:
                    for client in self.clients:
                        if client.connected:
                            if client.streamQueue.empty():  # discard any images while the queue is not empty
                                client.bufferStreamData(data)
                            else:
                                # Dropping frame
                                pass
                        else:
                            client.logger.debug("Streaming client disconnected.")
                            self.clients.remove(client)
                except Exception:
                    raise
                
                self.lastFrame = data
                
                if not enableDetection:
                    continue 

        q_analyze.put("")
        for i in range(0, 5):
            if mp_analyze_process.is_alive():
                time.sleep(1)
            else:
                break

        if mp_analyze_process.is_alive():
            try:
                mp_analyze_process.terminate()
            except Exception:
                raise

        videoDevice.release()
        for item in threadPool:
            if not item.is_alive():
                item.join()
        
    def stream(self, httpRequest):
        """
        Adds a streaming client to the watcher device for MJPEG streaming output
        
        Args:
            httpRequest (KHTTPHandler): Handler for inbound request (from which to pull the socket).
            
        Returns:
            (bool): True on success or False on failure
            
        """
        
        client = TCPStreamingClient(httpRequest.socket)
        client.start()
        self.clients.append(client)
        httpRequest.isResponseSent = True

        return True
    
    def snapshot(self, httpRequest):
        """
        Displays the snapshot from the last frame
        
        Args:
            httpRequest (KHTTPHandler): Handler for inbound request (from which to pull the socket).
            
        Returns:
            (bool): True on success or False on failure
            
        """
        
        if self.lastFrame is None:
            return httpRequest.sendError()
        
        return httpRequest.sendHTTP(contentBody=self.lastFrame, contentType="image/jpeg")
    
    def accepts(self):
        return ["start", "stop", "stream", "snapshot"]
        
    def isRunning(self):
        """
        Identifies if the device is actively running.

        Returns:
            (bool):  True if running; False if not running.
        """
        
        return self._isRunning
    
    def stop(self, httpRequest=None):
        """
        Stops the watcher.  
        
        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.

        Returns:
            (bool):  True on success else will raise an exception.
        """
        
        for item in self.clients:
            item.kill = True

        if not self._isRunning:
            return True 

        self._isRunning = False
        if self.thread is not None:
            self.logger.debug("Waiting for threads to close.")
            self.thread.join()
            
        self.logger.debug("Stopped.")
        return True
        
    def start(self, httpRequest=None, useThreads=True):
        """
        Starts the watcher.

        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.
            useThreads (bool):  Indicates if the brain should be started on a new thread.
        
        Returns:
            (bool):  True on success else will raise an exception.
        """
        if self._isRunning:
            return True 
        
        self.thread = self._readFromCamera()
        if not useThreads:
            self.wait()
            
        return True
    
    def wait(self, seconds=0):
        """
        Waits for any active watchers to complete before closing.
        
        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.
            seconds (int):  Number of seconds to wait before calling the "stop()" function
            
        Returns:
            (bool):  True on success else will raise an exception.
        """
        if not self._isRunning:
            return True 
        
        if seconds > 0:
            if self.thread is not None:
                time.sleep(seconds)
                self.stop()
        
        else:
            if self.thread is not None:
                self.thread.join()
            
        return True


@pthreaded
def process(q, args, callback):
    lastFaceSeen = 0

    numObjects = 0
    numFaces = 0
    faceList = {}
    hasMotion = False
    
    timeDelay = 0
    fps = args.get("framesPerSecond")
    if fps is None:
        frameBuffer = None
    else:
        frameBuffer = collections.deque(maxlen=int(fps) * 5)

    enableRecording = args.get("enableRecording", False)
    videoFolder = args.get("videoFolder")
    if videoFolder is None:
        enableRecording = False

    try:
        os.makedirs(videoFolder, exist_ok=True)
    except Exception:
        raise

    videoWriter = None
    fourcc = cv2.VideoWriter_fourcc(*args.get("videoFormat", "XVID"))

    fileExtension = ".avi"
    if str(args.get("videoFormat", "XVID")).lower() == "mp4v":
        fileExtension = ".m4v"
    elif str(args.get("videoFormat", "XVID")).lower() == "h264":
        fileExtension = ".m4v"

    try:
        k_img = detector(**args)
    except Exception:
        logging.debug(str(sys.exc_info()[0]))
        logging.debug(str(traceback.format_exc()))
        logging.info("Detector failed to initialize.")
        return

    if isinstance(args.get("faceImages"), list):
        pass
    elif isinstance(args.get("faceImages"), dict):
        faces = args.get("faceImages")
        for name in faces:
            if isinstance(faces[name], list):
                for img_nm in faces[name]:
                    if os.path.isfile(img_nm):
                        k_img.addFace(img_nm, name)
            elif isinstance(faces[name], str):
                img_nm = faces[name]
                if os.path.isfile(img_nm):
                    k_img.addFace(img_nm, name)

    while True:
        doNotify = False
        data = q.get()
        if data is None or len(data) <= 0 or not isinstance(data, dict):
            return

        ctime = time.time()
        if data["time"] < ctime - 0.1:
            continue 

        try:
            im = data["img"]

            detectFaces = True if k_img._detectFaces and ctime - 0.5 < lastFaceSeen else False
            k_img.analyze(im, detectFaces=detectFaces)
            objListArr = [x["name"] for x in k_img.objects]

            lastFaceSeen = ctime

            if enableRecording and k_img._detectObjects and "person" in k_img._objList:
                if frameBuffer is not None:
                    people = [x["name"] for x in k_img.objects]
                    if "person" in people:
                        timeDelay = data["time"]
                        if videoWriter is None:
                            fileName = os.path.join(
                                videoFolder, 
                                time.strftime("%Y%m%d", time.gmtime(data["time"])), 
                                "vid_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime(data["time"])) + fileExtension
                            )

                            try:
                                os.makedirs(os.path.dirname(fileName), exist_ok=True)
                            except Exception:
                                raise

                            videoWriter = cv2.VideoWriter(
                                os.path.join(fileName), 
                                fourcc, 
                                fps, 
                                (im.shape[1], im.shape[0])
                            )
                        
                        for item in frameBuffer:
                            videoWriter.write(item)

                        frameBuffer.clear()
                        videoWriter.write(im)

                    else:
                        if timeDelay != 0 and data["time"] > timeDelay + 5 and videoWriter is not None:
                            for item in frameBuffer:
                                videoWriter.write(item)

                            videoWriter.write(im)
                            videoWriter.release()
                            frameBuffer.clear()
                            videoWriter = None
                        else:
                            frameBuffer.append(im)
                else:
                    if timeDelay == 0:
                        timeDelay = data["time"]
                    else:
                        try:
                            fps = 1 / (data["time"] - timeDelay)
                            frameBuffer = collections.deque(maxlen=int(fps) * 5)
                        except Exception:
                            pass

            if k_img._detectMotion and len(k_img.movements) > 0:
                if not hasMotion:
                    hasMotion = True
                    doNotify = True
            else:
                if hasMotion:
                    hasMotion = False
                    doNotify = True

            objListArr = []
            numobj = len(k_img.objects)
            if numObjects != numobj:
                numObjects = numobj
                doNotify = True

            for x in k_img.faces:
                if x["name"] is not None:
                    faceList[x["name"]] = data["time"]

            faceListArr = []
            for x in faceList:
                if faceList[x] > ctime - 1:
                    faceListArr.append(x)

            numface = len(k_img.faces)
            if len(faceListArr) > 0:
                numface = len(faceListArr) if numface < len(faceListArr) else numface

            if detectFaces and numFaces != numface:
                numFaces = numface
                doNotify = True

            if doNotify:
                if numObjects > 0:
                    objListArr = [x["name"] for x in k_img.objects]

                callback({
                    "hasMotion": hasMotion,
                    "numObjects": numObjects,
                    "objectList": objListArr,
                    "numFaces": numFaces,
                    "faceList": faceListArr,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(data["time"]))
                })

        except Exception:
            logging.debug(str(sys.exc_info()[0]))
            logging.debug(str(traceback.format_exc()))
            logging.info("Detector failed to process image.")
            pass
