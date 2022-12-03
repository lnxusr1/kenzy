import os
import logging
import numpy as np
import time
import json
from kenzy.shared import threaded, TCPStreamingClient
from kenzy import GenericDevice
import cv2 
from PIL import Image


class Watcher(GenericDevice):
    """
    Watcher device to capture and process inbound video stream for objects and faces.
    """
    
    def __init__(self, **kwargs):
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

        super(Watcher, self).__init__(**kwargs)
        
    def updateSettings(self):

        self.parent = self.args.get("parent")
        self._callbackHandler = self.args.get("callback")                       # Callback function accepts two positional args (Type, Text)
        
        fPath = os.path.join(os.path.dirname(__file__), "..", "data", "models", "watcher")
        self.classifierFile = self.args.get("classifierFile", os.path.abspath(os.path.join(fPath, "haarcascade_frontalface_default.xml")))
        
        hPath = os.path.join(os.path.expanduser("~/.kenzy"), "data", "models", "watcher")
        self.recognizerFile = self.args.get("recognizerFile", os.path.abspath(os.path.join(hPath, "recognizer.yml")))

        self.namesFile = self.args.get("namesFile", os.path.abspath(os.path.join(hPath, "names.json")))

        if not os.path.isdir(os.path.dirname(self.namesFile)):
            try:
                self.logger.debug("Attempting to create names file location.")
                os.makedirs(os.path.dirname(self.namesFile), exist_ok=True)
            except Exception:
                self.logger.debug("Unable to create names file location.")
                pass
        
        self.trainingSourceFolder = self.args.get("trainingSourceFolder")
        self.videoDeviceIndex = self.args.get("videoDeviceIndex", 0)
        self.framesPerSecond = float(self.args.get("framesPerSecond", 29.97))
        
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

        if self.classifierFile is None or not os.path.isfile(self.classifierFile):
            self.logger.error("Invalid classifier file specified. Unable to start Watcher.")
            self.classifierFile = None 
            self._isRunning = False
            return False
        
        enableDetection = True
        
        try:
            classifier = cv2.CascadeClassifier(self.classifierFile)
            recognizer = cv2.face.LBPHFaceRecognizer_create()
        except Exception:
            self.logger.warning("OpenCV does not have support for detection/recognition.  Reverting to simple image capture device.")
            enableDetection = False
            pass

        if enableDetection:        
            if self.recognizerFile is None or not os.path.isfile(self.recognizerFile):
                if self.classifierFile is not None and self.trainingSourceFolder is not None and os.path.isdir(self.trainingSourceFolder):
                    self.logger.info("Recognizer file not found.  Will attempt to generate.")
                    if not self.train():
                        self.logger.critical("Unable to start watcher due to failed recognizer build.")
                        self._isRunning = False
                        return False 
                else:
                    self.logger.warning("Invalid recognizer file and no training source was provided. Named objects will not be detected.")
                    recognizer = None
            else:
                recognizer.read(self.recognizerFile)
            
        names = { }
        if self.namesFile is not None and os.path.isfile(self.namesFile):
            with open(self.namesFile, 'r') as fp:
                obj = json.load(fp)
            
            if isinstance(obj, list):
                for item in obj:
                    if "id" in item and "name" in item:
                        names[item["id"]] = item["name"]
            
        isPaused = False 
        
        videoDevice = cv2.VideoCapture(self.videoDeviceIndex)

        threadPool = []
        
        lTime = 0  # should be current time but we need to seed the first image.
        yTime = 0
        
        # Test camera to make sure we can read a frame
        ret, im = videoDevice.read()
        if not ret:
            self.logger.error("Unable to read from camera device. Is the device connected?")
            self._isRunning = False
            self.stop()
            return False

        self._isRunning = True
        
        while self._isRunning:
            ret, im = videoDevice.read()
            if ret:
                
                t = time.time()
                if t < (yTime + 0.05):  # No more than 20 frames per second
                    continue 
                
                yTime = t 
                
                # See if we need to rotate it and do so if required
                if self.orientation is not None:
                    im = cv2.rotate(im, self.orientation)

                width = int(im.shape[1])
                height = int(im.shape[0])
                
                if width > 640:
                    width = 640
                    height = int(im.shape[0]) / int(im.shape[1]) * width
                    
                if height > 480:
                    height = 480
                    width = int(im.shape[1]) / int(im.shape[0]) * height
                
                dim = (width, height)
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
                
                if t < (lTime + 1):
                    continue
                
                lTime = t
                self.lastFrame = data
                
                if not enableDetection:
                    continue 
                
                # Convert image to grayscale.  
                # Some folks believe this improves identification, but your mileage may vary.
                gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
                
                # Detect faces (not the who... just if I see a face).
                # Returns an array for each face it sees in the frame.
                faces = classifier.detectMultiScale(gray, 1.2, 5)
                
                # Since we care about all the faces we'll store them after they are processed in an array
                people = []
                
                # Iterate through the faces for identification.
                for (x, y, w, h) in faces:
    
                    # Pull the ID and Distance from the recognizer based on the face in the image
                    # Remember that "gray" is our image now so this is literally cutting out the face
                    # at the coordinates provided and attempting to predict the person it is seeing.
                    
                    if recognizer is None:
                        Id = [0, 0]
                    else:
                        Id = recognizer.predict(gray[y:y + h, x:x + w])

                    # Let's build a JSON array of the person based on what we've learned so far.
                    person = {
                        "id": Id[0],
                        "name": names[Id[0]] if Id[0] in names else "",
                        "distance": Id[1],
                        "coordinates": {
                            "x": int(x),
                            "y": int(y)
                        },
                        "dimensions": {
                            "width": int(w),
                            "height": int(h)
                        }
                    }
    
                    # And now we save our person to our array of people.
                    people.append(person)
                    isPaused = False  # Used to send the latest frame, even if no people are present
                
                # Send the list of people in the frame to the brain.
                # We do this on a separate thread to avoid blocking the image capture process.
                # Technically we could have offloaded the entire recognizer process to a separate 
                # thread so may need to consider doing that in the future.
                if (len(people) > 0) or not isPaused:
                    # We only send data to the brain when we have something to send.
                    t = self._doCallback(people) 
                    
                    i = len(threadPool) - 1
                    while i >= 0:
                        try:
                            if not threadPool[i].is_alive():
                                threadPool[i].join()
                                threadPool.pop(i)
                        except Exception:
                            pass
                            
                        i = i - 1
                    
                    threadPool.append(t)
                    isPaused = True  # Set to pause unless I have people.
                    
                if (len(people) > 0):
                    isPaused = False  # Need to sort out the logic b/c we shouldn't have to count the array again.

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
    
    def train(self, httpRequest=None, trainingSourceFolder=None):
        """
        Retrains the face recognition based on images in the supplied folder
        
        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.
            trainingSourceFolder (str): The source directory that contains all the images to use for building a new 
                                        recognizerFile.  Will use the configuration value if the input value is left 
                                        empty. (optional)
            
        Returns:
            (bool): True on success or False on failure
            
        """
        if trainingSourceFolder is not None:
            self.trainingSourceFolder = trainingSourceFolder
        
        if self.trainingSourceFolder is None or not os.path.isdir(self.trainingSourceFolder):
            self.logger.error("Invalid training source folder specified.  Unable to retrain recognizer file.")
            return False
        
        self.logger.debug("Using " + str(self.trainingSourceFolder) + " for building recognizer file.")
        
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        classifier = cv2.CascadeClassifier(self.classifierFile)
        
        samples = []
        ids = []
        names = []

        namePaths = sorted([f.path for f in os.scandir(self.trainingSourceFolder) if f.is_dir()])
        for i, entry in enumerate(namePaths):
            names.append({ "id": (i + 1), "name": os.path.basename(entry) })
            self.logger.info("Processing " + os.path.basename(entry) + " directory")
            imagePaths = sorted([f.path for f in os.scandir(entry) if f.is_file()])

            # Loop through input images in the folder supplied.
            for imagePath in imagePaths:
                
                try:
                    # Open the image as a resource
                    PIL_img = Image.open(imagePath).convert('L')
                
                    # Convert to Numpy Array
                    img_numpy = np.array(PIL_img, 'uint8')
                
                    # At this point we should be okay to proceed with the image supplied.
                    self.logger.debug("Processing " + imagePath)
                
                    # Let's pull out the faces from the image (may be more than one!)
                    faces = classifier.detectMultiScale(img_numpy)
            
                    # Loop through faces object for detection ... and there should only be 1. 
                    for (x, y, w, h) in faces:
                    
                        # Let's save the results of what we've found so far.
                    
                        # Yes, we are cutting out the face from the image and storing in an array.
                        samples.append(img_numpy[y:y + h, x:x + w]) 
                    
                        # Ids go in the ID array.
                        ids.append(i + 1)
                except Exception:
                    self.logger.error("Failed to process: " + imagePath)
                    raise

        # Okay, we should be done collecting faces.
        self.logger.info("Identified " + str(len(samples)) + " sample images")
        
        # This is where the real work happens... let's create the training data based on the faces collected.
        recognizer.train(samples, np.array(ids))

        # And now for the final results and saving them to a file.
        self.logger.debug("Writing data to " + self.recognizerFile)
        recognizer.save(self.recognizerFile)
        
        self.logger.debug("Writing data to " + self.namesFile)
        with open(self.namesFile, 'w') as fp:
            json.dump(names, fp)
        
        self.logger.info("Training algorithm completed.")
        
        return True
    
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
