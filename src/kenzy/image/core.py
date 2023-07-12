import os
import face_recognition
import cv2
import numpy as np
import time
import logging


class detector(object):
    def __init__(self, **kwargs):

        self.logger = logging.getLogger("DETECTOR")
        self.rt_logger = logging.getLogger("DETECT_TIME")

        self._imageMarkup = kwargs.get("imageMarkup", True)
        self._imageMarkupFaces = kwargs.get("imageMarkupFaces", self._imageMarkup)
        self._imageMarkupObjects = kwargs.get("imageMarkupObjects", self._imageMarkup)
        self._imageMarkupMotion = kwargs.get("imageMarkupMotion", self._imageMarkup)

        self._faceNames = []
        self._faceEncodings = []

        orientation = int(kwargs.get("orientation", "0"))  # 0, 90, 180, 270
        self._orientation = None
        if orientation == 90:
            self._orientation = cv2.ROTATE_90_CLOCKWISE
            self._isRotated = False
        elif orientation == 180:
            self._orientation = cv2.ROTATE_180
            self._isRotated = False
        elif orientation == 270 or orientation == -90:
            self._orientation = cv2.ROTATE_90_COUNTERCLOCKWISE
            self._isRotated = False
        
        self._detectFaces = kwargs.get("detectFaces", True)
        self._recognizeFaces = False
        self._detectObjects = kwargs.get("detectObjects", True)
        self._detectMotion = kwargs.get("detectMotion", True)
        
        self.logger.info("Face Detection     = " + str("Enabled" if self._detectFaces else "Disabled"))
        self.logger.info("Object Detection   = " + str("Enabled" if self._detectObjects else "Disabled"))
        self.logger.info("Motion Detection   = " + str("Enabled" if self._detectMotion else "Disabled"))
        
        self._faceScaleDownFactor = float(kwargs.get("scaleFactor", "1.0"))  # (1.0 >= VALUE > 0)
        self._faceModel = kwargs.get("faceModel", "hog")  # hog or cnn
        self._faceScaleUpFactor = 1.0  
        self._defaultFaceName = kwargs.get("defaultFaceName", "Unknown")
        self._faceShowNames = kwargs.get("showFaceNames", True)
        self._faceOutlineColor = kwargs.get("faceOutlineColor", (0, 0, 255))
        self._faceFontColor = kwargs.get("faceFontColor", (255, 255, 255))
        
        if self._faceScaleDownFactor > 0 and self._faceScaleDownFactor != 1.0:
            self._faceScaleUpFactor = ((1.0 - float(self._faceScaleDownFactor)) / float(self._faceScaleDownFactor)) + 1.0
            if self._faceScaleDownFactor > 1:
                x = self._faceScaleUpFactor
                self._faceScaleUpFactor = self._faceScaleDownFactor
                self._faceScaleDownFactor = x

        self.image = None
        self._lastImage = None
        self._scaledBGRImage = None
        self._scaledRGBImage = None
        self._scaledBWImage = None
        self._lastScaledBWImage = None

        self._objList = kwargs.get("objDetectList")
        if self._objList is not None and isinstance(self._objList, str):
            self._objList = [str(x).lower().strip() for x in self._objList.split(",") if str(x).lower().strip() != ""]
        if self._objList is not None and not isinstance(self._objList, list):
            self._objList = None

        self._objModelType = kwargs.get("objModelType", "ssd")
        self._objDetectCfg = {}

        if self._objModelType.strip().lower() == "yolo":
            self._objConfigFile = kwargs.get("objDetectCfg", os.path.join(os.path.dirname(__file__), 
                                                                          "resources", 
                                                                          "yolov7", 
                                                                          "config.json"))
            
            self._objModelFile = kwargs.get("objDetectModel", os.path.join(os.path.dirname(__file__), 
                                                                           "resources", 
                                                                           "yolov7", 
                                                                           "yolov7-tiny.pt"))
            
            self._objLabelFile = kwargs.get("objDetectLabels", os.path.join(os.path.dirname(__file__), 
                                                                            "resources", 
                                                                            "yolov7", 
                                                                            "labels.txt"))
            
            if os.path.isfile(self._objConfigFile):
                import json
                with open(self._objConfigFile, "r", encoding="UTF-8") as fp:
                    self._objDetectCfg = json.load(fp)

            exec("import " + self._objDetectCfg.get("library", "yolov7"))
            self._objModel = eval(self._objDetectCfg.get("library", "yolov7") + ".load(self._objModelFile)")
            self._objModel.conf = self._objDetectCfg.get("confidence", 0.25)
            self._objModel.iou = self._objDetectCfg.get("iou", 0.45)

        else:
            self._objConfigFile = kwargs.get("objDetectCfg", os.path.join(os.path.dirname(__file__), 
                                                                          "resources", 
                                                                          "mobilenet_v3", 
                                                                          "ssd_mobilenet_v3_large_coco_2020_01_14.pbtxt"))
            
            self._objModelFile = kwargs.get("objDetectModel", os.path.join(os.path.dirname(__file__), 
                                                                           "resources", 
                                                                           "mobilenet_v3", 
                                                                           "frozen_inference_graph.pb"))
            
            self._objLabelFile = kwargs.get("objDetectLabels", os.path.join(os.path.dirname(__file__), 
                                                                            "resources", 
                                                                            "mobilenet_v3", 
                                                                            "labels.txt"))

            self._objModel = cv2.dnn_DetectionModel(self._objModelFile, self._objConfigFile)
            self._objModel.setInputSize(320, 320)  # greater this value the better the results; tune it for best output
            self._objModel.setInputScale(1.0 / 127.5)
            self._objModel.setInputMean((127.5, 127.5, 127.5))
            self._objModel.setInputSwapRB(True)

        self._objShowNames = kwargs.get("showObjectNames", True)
        self._objOutlineColor = kwargs.get("objOutlineColor", (255, 0, 0))
        self._objFontColor = kwargs.get("objFontColor", (255, 255, 255))

        self._objLabels = []
        
        self._motionThreshold = kwargs.get("motionThreshold", 20)
        self._motionMinArea = kwargs.get("motionMinArea", 50)
        self._motionOutlineColor = kwargs.get("motionOutlineColor", (0, 255, 0))

        self.logger.debug("==< CONFIG >===================================")
        self.logger.debug("orientation        = " + str(orientation))
        self.logger.debug("imageMarkupFaces   = " + str(self._imageMarkupFaces))
        self.logger.debug("imageMarkupObjects = " + str(self._imageMarkupObjects))
        self.logger.debug("imageMarkupMotion  = " + str(self._imageMarkupMotion))
        self.logger.debug("detectFaces        = " + str(self._detectFaces))
        self.logger.debug("detectObjects      = " + str(self._detectObjects))
        self.logger.debug("detectMotion       = " + str(self._detectMotion))
        self.logger.debug("scaleFactor        = " + str(self._faceScaleDownFactor))
        self.logger.debug("faceModel          = " + str(self._faceModel))
        self.logger.debug("defaultFaceName    = " + str(self._defaultFaceName))
        self.logger.debug("showFaceNames      = " + str(self._faceShowNames))
        self.logger.debug("faceFontColor      = " + str(self._faceFontColor))
        self.logger.debug("faceOutlineColor   = " + str(self._faceOutlineColor))

        self.logger.debug("objModelType       = " + str(self._objModelType))
        self.logger.debug("objDetectCfg       = " + str(self._objConfigFile))
        self.logger.debug("objDetectList      = " + str(self._objList))
        self.logger.debug("objDetectModel     = " + str(self._objModelFile))  
        self.logger.debug("objDetectLabels    = " + str(self._objLabelFile))  
        self.logger.debug("showObjectNames    = " + str(self._objShowNames))
        self.logger.debug("objFontColor       = " + str(self._objFontColor))
        self.logger.debug("objOutlineColor    = " + str(self._objOutlineColor))
        
        self.logger.debug("motionThreshold    = " + str(self._motionThreshold))
        self.logger.debug("motionMinArea      = " + str(self._motionMinArea))
        self.logger.debug("motionOutlineColor = " + str(self._motionOutlineColor))
        self.logger.debug("==</ CONFIG >==================================")

        self.faces = []
        self.objects = []
        self.movements = []

        self._loadLabels()
        
    def addFace(self, fileName, faceName):
        if not os.path.isfile(fileName):
            raise FileNotFoundError("Face file not found.")

        newFaceImage = face_recognition.load_image_file(fileName)
        newFaceEncoding = face_recognition.face_encodings(newFaceImage)[0]

        self._faceEncodings.append(newFaceEncoding)
        self._faceNames.append(str(faceName) if faceName is not None else "")

        self.logger.info("Adding face (" + str(faceName) + "): " + str(fileName))
        
        self.logger.debug("Enabling face detection because face image has been added")
        self._detectFaces = True
        self._recognizeFaces = True

        return True
        
    def _formatImage(self, image=None):
        if image is None:
            return

        self._lastScaledBWImage = self._scaledBWImage
        self.image = image
        
        if isinstance(image, str):
            if os.path.isfile(image):
                self.image = cv2.imread(image)
            else:
                raise FileNotFoundError("Image not found for analysis.")

        if self._orientation is not None:
            self.image = cv2.rotate(self.image, self._orientation)

        self._scaledBGRImage = None
        self._scaleImage()

        self._scaledBWImage = None
        self._bwImage()

    def _scaleImage(self):
        if self._scaledBGRImage is None:
            if self._faceScaleDownFactor != 1.0:
                self._scaledBGRImage = cv2.resize(self.image, (0, 0), fx=self._faceScaleDownFactor, fy=self._faceScaleDownFactor, interpolation=cv2.INTER_AREA)
            else:
                self._scaledBGRImage = self.image.copy()

            self._scaledRGBImage = cv2.cvtColor(self._scaledBGRImage, cv2.COLOR_BGR2RGB)
    
    def _bwImage(self):
        self._scaleImage()
        if self._scaledBWImage is None:
            self._scaledBWImage = cv2.cvtColor(self._scaledBGRImage, cv2.COLOR_BGR2GRAY)
            self._scaledBWImage = cv2.GaussianBlur(src=self._scaledBWImage, ksize=(5, 5), sigmaX=0)

    def _loadLabels(self):
        self.logger.debug("Loading label file from " + str(self._objLabelFile))
        with open(self._objLabelFile, 'rt') as fp:
            self._objLabels = fp.read().rstrip('\n').split('\n')

    def analyze(self, image, detectFaces=None, detectObjects=None, detectMotion=None):
        """
        Set detectFaces, detectObjects, or detectMotion to True or False to override global setting for this one image.
        """

        start = time.time()

        self.faces = []
        self.objects = []
        self.movements = []
        self._isRotated = False if self._orientation is not None else True

        self._formatImage(image)
        
        if (detectMotion is None and self._detectMotion) or detectMotion:
            self.motion_detection()

        if (detectObjects is None and self._detectObjects) or detectObjects:
            self.object_detection()

        if (detectFaces is None and self._detectFaces) or detectFaces:
            # self.face_detection(filterByObjects=detectFaces if detectFaces is not None else self._detectObjects)
            
            # Hack to disable optimizations preventing multi-channel 
            # images from processing correctly in face recognition
            self.face_detection(filterByObjects=False)

        end = time.time()
        
        self.rtSecs = end - start
        self.rt_logger.debug("Executed in " + str(self.rtSecs) + " seconds")

    def _get_face_parts(self, im, offset_top=None, offset_left=None):
        # Find face outline
        face_locations = face_recognition.face_locations(im, model=self._faceModel)
        if face_locations is None or len(face_locations) < 1:
            return [], None

        face_names = None
        if self._recognizeFaces:
            # Determine whose face this is
            face_encodings = face_recognition.face_encodings(im, face_locations)

            face_names = []
            for face_encoding in face_encodings:
                matches = face_recognition.compare_faces(self._faceEncodings, face_encoding)
                name = self._defaultFaceName

                face_distances = face_recognition.face_distance(self._faceEncodings, face_encoding)
                best_match_index = np.argmin(face_distances)
                if matches[best_match_index]:
                    name = self._faceNames[best_match_index]

                face_names.append(name)

        if offset_top is not None and offset_left is not None:
            fl = []
            for item in face_locations:
                # Relocate using top left corner of cropped image
                fl.append((offset_top + item[0], offset_left + item[1], offset_top + item[2], offset_left + item[3]))

            face_locations = fl            
        
        return face_locations, face_names

    def face_detection(self, image=None, filterByObjects=False):

        start = time.time()

        self._formatImage(image)
        self.faces = []

        face_locations = []
        face_names = []

        # Optimization:  
        # If we have enabled object detection but we don't find any "person" then we can skip to the end.
        if filterByObjects and "person" in self._objLabels:
            if "person" not in [x.get("name") for x in self.objects]:
                return
            
            else:
                # Theoretically, since we can do object detection for "person" then we could carve out of the
                # main picture the "person" entries and feed those individually which should make the whole
                # process run faster since it'd be covering fewer pixels... thus enabling larger scale images.
                #
                # Obviously this only works if detect objects is enabled and we include person detection there.
                # So to make this "real" we would need a new variable to use object/face optimizations while
                # still enabling the end user to supply their own inference model which may not map person or
                # may map it in an incompatible way.

                # TODO: FIX THIS (currently disabled due to multi-channel image issues in face recognition)

                for item in self.objects:
                    if item.get("name").lower().strip() == "person":

                        loc = item["scaled_location"]
                        left = loc["left"]
                        top = loc["top"]
                        right = loc["right"]
                        bottom = loc["bottom"]
                        im = self._scaledRGBImage[top:bottom, left:right]

                        fl, fn = self._get_face_parts(im, top, left)
                        face_locations.extend(fl)
                        if fn is None:
                            face_names.append(None)
                        else:
                            face_names.extend(fn)

        else:
            # Default (if detectObjects is disabled and filterByObjects is False)
            fl, fn = self._get_face_parts(self._scaledRGBImage)
            face_locations.extend(fl)
            if fn is None:
                face_names.append(None)
            else:
                face_names.extend(fn)

        for idx, (stop, sright, sbottom, sleft) in enumerate(face_locations):
            left = int(sleft * self._faceScaleUpFactor)
            top = int(stop * self._faceScaleUpFactor)
            right = int(sright * self._faceScaleUpFactor)
            bottom = int(sbottom * self._faceScaleUpFactor)

            face_name = face_names[idx] if self._recognizeFaces else self._defaultFaceName

            if self._imageMarkupFaces:
                cv2.rectangle(self.image, (left, top), (right, bottom), self._faceOutlineColor, 2)

                if self._recognizeFaces and self._faceShowNames:
                    cv2.rectangle(self.image, (left, bottom - 18), (right, bottom), self._faceOutlineColor, cv2.FILLED)
                    font = cv2.FONT_HERSHEY_DUPLEX
                    cv2.putText(self.image, face_names[idx] if face_names is not None else "", (left + 6, bottom - 6), font, 0.5, self._faceFontColor, 1)

            if isinstance(self.faces, list):
                self.faces.append({ 
                    "type": "Face", 
                    "confidence": 1.0, 
                    "name": face_name, 
                    "location": { 
                        "left": left, 
                        "top": top, 
                        "right": right, 
                        "bottom": bottom 
                    },
                    "scaled_location": { 
                        "left": sleft, 
                        "top": stop, 
                        "right": sright, 
                        "bottom": sbottom 
                    } 
                })
        
        end = time.time()
        self.rt_logger.debug("FACE TIME   = " + str(end - start) + " seconds")

    def _parse_obj_detect_info(self, classInd, conf, bounding_box):
        # bounding_box = (left, top, width, height)

        class_name = None

        if classInd >= 0 and classInd < len(self._objLabels):
            class_name = self._objLabels[classInd]

        if self._objList is not None:
            if class_name is None or str(class_name).strip().lower() not in self._objList:
                return

        sleft = bounding_box[0]
        stop = bounding_box[1]
        sright = bounding_box[0] + bounding_box[2]
        sbottom = bounding_box[1] + bounding_box[3]

        left = int(sleft * self._faceScaleUpFactor)
        top = int(stop * self._faceScaleUpFactor)
        right = int(sright * self._faceScaleUpFactor)
        bottom = int(sbottom * self._faceScaleUpFactor)

        if isinstance(self.objects, list):
            self.objects.append({ 
                "type": "Object", 
                "confidence": conf, 
                "name": class_name,
                "location": { 
                    "left": left, 
                    "top": top, 
                    "right": right, 
                    "bottom": bottom 
                },
                "scaled_location": { 
                    "left": sleft, 
                    "top": stop, 
                    "right": sright, 
                    "bottom": sbottom 
                } 
            })

        if self._imageMarkupObjects:
            cv2.rectangle(self.image, (left, top), (right, bottom), self._objOutlineColor, 2)
            if class_name is not None and self._objShowNames:
                cv2.rectangle(self.image, (left, bottom - 18), (right, bottom), self._objOutlineColor, cv2.FILLED)
                font = cv2.FONT_HERSHEY_DUPLEX
                cv2.putText(self.image, class_name, (left + 6, bottom - 6), font, 0.5, self._objFontColor, 1)

    def object_detection(self, image=None):
        start = time.time()

        self._formatImage(image)

        self.objects = []

        if self._objModelType == "yolo":
            results = self._objModel(self._scaledBGRImage, 
                                     size=int(self._objDetectCfg.get("size", 640)), 
                                     augment=self._objDetectCfg.get("augment"))

            predictions = results.pred[0]
            for item in predictions:
                bounding_box = (int(item[0]), int(item[1]), int(item[2]) - int(item[0]), int(item[3]) - int(item[1]))
                conf = item[4]
                classInd = int(item[5])
                self._parse_obj_detect_info(classInd, conf, bounding_box)
        else:
            classIndex, confidence, bbox = self._objModel.detect(self._scaledBGRImage, confThreshold=0.5)

            try:
                for classInd, conf, bounding_box in zip(classIndex.flatten(), confidence.flatten(), bbox):
                    # classInd = index starting at 1 for ssd/mobilenet so we need to subtract one from it
                    self._parse_obj_detect_info(classInd - 1, conf, bounding_box)

            except AttributeError:
                pass
        
        end = time.time()
        self.rt_logger.debug("OBJ TIME    = " + str(end - start) + " seconds")

    def motion_detection(self, image=None):
        
        start = time.time()

        self._formatImage(image)

        self.movements = []

        self._scaledBWImage = cv2.cvtColor(self._scaledBGRImage, cv2.COLOR_BGR2GRAY)
        self._scaledBWImage = cv2.GaussianBlur(src=self._scaledBWImage, ksize=(5, 5), sigmaX=0)

        if self._lastScaledBWImage is None:
            return
        
        diff_frame = cv2.absdiff(src1=self._scaledBWImage, src2=self._lastScaledBWImage)

        kernel = np.ones((5, 5))
        diff_frame = cv2.dilate(diff_frame, kernel, 1)

        thresh_frame = cv2.threshold(src=diff_frame, thresh=self._motionThreshold, maxval=255, type=cv2.THRESH_BINARY)[1]
        contours, _ = cv2.findContours(image=thresh_frame, mode=cv2.RETR_EXTERNAL, method=cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            if cv2.contourArea(contour) < self._motionMinArea:
                # too small: skip!
                continue

            (x, y, w, h) = cv2.boundingRect(contour)

            if isinstance(self.movements, list):
                self.movements.append({ 
                    "type": "Movement", 
                    "confidence": 1.0, 
                    "location": { 
                        "left": int(float(x) * self._faceScaleUpFactor), 
                        "top": int(float(y) * self._faceScaleUpFactor), 
                        "right": int(float(x + w) * self._faceScaleUpFactor), 
                        "bottom": int(float(y + h) * self._faceScaleUpFactor) 
                    },
                    "scaled_location": { 
                        "left": int(x), 
                        "top": int(y), 
                        "right": int(x + w), 
                        "bottom": int(y + h)
                    } 
                })

            if self._imageMarkupMotion:
                cv2.rectangle(img=self.image, 
                              pt1=(int(float(x) * self._faceScaleUpFactor), int(float(y) * self._faceScaleUpFactor)), 
                              pt2=(int(float(x) * self._faceScaleUpFactor) + int(float(w) * self._faceScaleUpFactor), 
                                   int(float(y) * self._faceScaleUpFactor) + int(float(h) * self._faceScaleUpFactor)), 
                              color=self._motionOutlineColor, 
                              thickness=2)
        
        end = time.time()
        self.rt_logger.debug("MOTION TIME = " + str(end - start) + " seconds")
