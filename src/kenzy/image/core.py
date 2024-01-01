import cv2
import numpy as np
import os
import face_recognition
import logging
import uuid
import kenzy.settings


def image_gray(image=None):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def image_blur(image=None):
    return cv2.GaussianBlur(src=image, ksize=(5, 5), sigmaX=0)


def motion_detection(image=None, last_image=None, threshold=20, motion_area=0.0003):
        
    movements = []

    if last_image is None:
        return []
    
    motion_area = image.shape[1] * image.shape[0] * motion_area
    
    diff_frame = cv2.absdiff(src1=image, src2=last_image)

    kernel = np.ones((5, 5))
    diff_frame = cv2.dilate(diff_frame, kernel, 1)

    thresh_frame = cv2.threshold(src=diff_frame, thresh=threshold, maxval=255, type=cv2.THRESH_BINARY)[1]
    contours, _ = cv2.findContours(image=thresh_frame, mode=cv2.RETR_EXTERNAL, method=cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        if cv2.contourArea(contour) < motion_area:
            # too small: skip!
            continue

        (x, y, w, h) = cv2.boundingRect(contour)

        if isinstance(movements, list):
            movements.append({ 
                "type": "movement", 
                "confidence": 1.0, 
                "location": { 
                    "left": int(x), 
                    "top": int(y), 
                    "right": int(x + w), 
                    "bottom": int(y + h) 
                } 
            })

    return movements


def image_markup(image, elements=[], line_color=(255, 0, 0)):
    for item in elements:
        x = item.get("location").get("left")
        y = item.get("location").get("top")
        w = item.get("location").get("right") - x
        h = item.get("location").get("bottom") - y

        cv2.rectangle(
            img=image, 
            pt1=(int(x), int(y)), 
            pt2=(int(x) + int(w), 
                 int(y) + int(h)), 
            color=line_color, 
            thickness=2
        )


def object_labels(label_file=None, model_type="ssd"):
    if label_file is None:
        if model_type is None:
            model_type = "ssd"

        if model_type == "yolo":
            label_file = os.path.join(os.path.dirname(__file__), 
                                      "resources", 
                                      "yolov7", 
                                      "labels.txt")
        else:
            label_file = os.path.join(os.path.dirname(__file__), 
                                      "resources", 
                                      "mobilenet_v3", 
                                      "labels.txt")
            
    labels = []
    if label_file is not None and os.path.isfile(label_file):
        with open(label_file, "r", encoding="UTF-8") as fp:
            for line in fp:
                labels.append(line.strip())

    return labels


def object_model(model_type="ssd", model_config=None, model_file=None):
    ret = None

    if model_type == "yolo":
        if model_config is None:
            model_config = os.path.join(
                os.path.dirname(__file__), 
                "resources", 
                "yolov7", 
                "config.json")
            
        if model_file is None:
            model_file = os.path.join(
                os.path.dirname(__file__), 
                "resources", 
                "yolov7", 
                "yolov7-tiny.pt")
        
        if os.path.isfile(model_config):
            import json
            with open(model_config, "r", encoding="UTF-8") as fp:
                cfg = json.load(fp)

        exec("import " + cfg.get("library", "yolov7"))
        model = eval(cfg.get("library", "yolov7") + ".load(model_file)")
        model.conf = cfg.get("confidence", 0.25)
        model.iou = cfg.get("iou", 0.45)

        ret = { "model": model, "config": cfg, "type": "yolo" }

    elif model_type == "ssd":
        if model_config is None:
            model_config = os.path.join(
                os.path.dirname(__file__), 
                "resources", 
                "mobilenet_v3", 
                "ssd_mobilenet_v3_large_coco_2020_01_14.pbtxt")
            
        if model_file is None:
            model_file = os.path.join(
                os.path.dirname(__file__), 
                "resources", 
                "mobilenet_v3", 
                "frozen_inference_graph.pb")

        model = cv2.dnn_DetectionModel(model_file, model_config)
        model.setInputSize(320, 320)  # greater this value the better the results; tune it for best output
        model.setInputScale(1.0 / 127.5)
        model.setInputMean((127.5, 127.5, 127.5))
        model.setInputSwapRB(True)

        ret = { "model": model, "config": {}, "type": "ssd" }

    return ret


def object_detection(image=None, model=None, labels=None, threshold=0.5, markup=False, line_color=(255, 0, 0), font_color=(255, 255, 255)):

    objects = []

    if image is None or model is None:
        return objects

    if model.get("type") == "yolo":
        results = model.get("model")(
            image, 
            size=int(model.get("config", {}).get("size", 640)), 
            augment=model.get("config", {}).get("augment"))

        predictions = results.pred[0]

        for item in predictions:
            conf = item[4]
            if conf < threshold:
                continue

            bounding_box = (int(item[0]), int(item[1]), int(item[2]) - int(item[0]), int(item[3]) - int(item[1]))
            classInd = int(item[5])

            class_name = None
            if labels is not None and classInd >= 0 and classInd < len(labels):
                class_name = labels[classInd]

            left = bounding_box[0]
            top = bounding_box[1]
            right = bounding_box[0] + bounding_box[2]
            bottom = bounding_box[1] + bounding_box[3]

            if isinstance(objects, list):
                objects.append({ 
                    "type": "object", 
                    "confidence": float(conf), 
                    "name": class_name,
                    "location": { 
                        "left": int(left), 
                        "top": int(top), 
                        "right": int(right), 
                        "bottom": int(bottom) 
                    } 
                })

            if markup:
                cv2.rectangle(image, (left, top), (right, bottom), line_color, 2)
                if class_name is not None:
                    cv2.rectangle(image, (left, bottom - 18), (right, bottom), line_color, cv2.FILLED)
                    font = cv2.FONT_HERSHEY_DUPLEX
                    cv2.putText(image, class_name, (left + 6, bottom - 6), font, 0.5, font_color, 1)

    else:
        classIndex, confidence, bbox = model.get("model").detect(image, confThreshold=threshold)

        try:
            for classInd, conf, bounding_box in zip(classIndex.flatten(), confidence.flatten(), bbox):
                class_name = None
                if labels is not None and classInd >= 0 and classInd < len(labels):
                    class_name = labels[classInd]

                left = bounding_box[0]
                top = bounding_box[1]
                right = bounding_box[0] + bounding_box[2]
                bottom = bounding_box[1] + bounding_box[3]

                if isinstance(objects, list):
                    objects.append({ 
                        "type": "object", 
                        "confidence": float(conf), 
                        "name": class_name,
                        "location": { 
                            "left": int(left), 
                            "top": int(top), 
                            "right": int(right), 
                            "bottom": int(bottom) 
                        } 
                    })

                if markup:
                    cv2.rectangle(image, (left, top), (right, bottom), line_color, 2)
                    if class_name is not None:
                        cv2.rectangle(image, (left, bottom - 18), (right, bottom), line_color, cv2.FILLED)
                        font = cv2.FONT_HERSHEY_DUPLEX
                        cv2.putText(image, class_name, (left + 6, bottom - 6), font, 0.5, font_color, 1)

        except AttributeError:
            pass
    
    return objects


def face_detection(image, model="hog", face_encodings=None, face_names=None, tolerance=0.6, default_name=None, 
                   markup=False, line_color=(255, 0, 0), font_color=(255, 255, 255), cache_folder=None):

    if default_name is None:
        default_name = "Unknown"

    faces = []
    face_locations = []

    # Find face outline
    face_locations = face_recognition.face_locations(image, model=model)
    if face_locations is None or len(face_locations) < 1:
        return []

    found_names = None
    found_distances = None

    if face_encodings is not None and face_names is not None:
        # Determine whose face this is
        fes = face_recognition.face_encodings(image, face_locations)

        found_names = []
        found_distances = []
        for idx, face_encoding in enumerate(fes):
            name = None
            best_match_index = None
            distance = None

            face_distances = face_recognition.face_distance(face_encodings, face_encoding)
            if face_distances is not None and len(face_distances) > 0:
                best_match_index = np.argmin(face_distances)
                if face_distances[best_match_index] < tolerance and best_match_index < len(face_names):
                    name = face_names[best_match_index]
                    distance = face_distances[best_match_index]
            
            if name is None:
                if best_match_index is not None and len(face_distances) > best_match_index and face_distances[best_match_index] > (tolerance * 1.5):
                    name = save_image_to_cache(image, face_position=face_locations[idx], cache_folder=cache_folder, 
                                               default_name=default_name, face_encoding=face_encoding, 
                                               known_face_encodings=face_encodings, face_names=face_names)
                elif best_match_index is not None and len(face_distances) > best_match_index and face_distances[best_match_index] < (tolerance * 1.5):
                    name = face_names[best_match_index]
                else:
                    name = default_name

            found_names.append(name)
            found_distances.append(distance)

    for idx, (top, right, bottom, left) in enumerate(face_locations):

        face_name = found_names[idx] if found_names is not None else default_name
        face_distance = found_distances[idx] if found_distances is not None else None

        if markup:
            cv2.rectangle(image, (left, top), (right, bottom), line_color, 2)

            if face_encodings is not None and face_names is not None:
                cv2.rectangle(image, (left, bottom - 18), (right, bottom), line_color, cv2.FILLED)
                font = cv2.FONT_HERSHEY_DUPLEX
                cv2.putText(image, found_names[idx] if found_names is not None else "", (left + 6, bottom - 6), font, 0.5, font_color, 1)

        faces.append({ 
            "type": "face", 
            "confidence": 1.0, 
            "distance": face_distance,
            "name": face_name, 
            "location": { 
                "left": left, 
                "top": top, 
                "right": right, 
                "bottom": bottom 
            }
        })
    
    return faces


def get_face_encoding(file_name):
    full_name = os.path.expanduser(file_name)

    if not os.path.isfile(full_name):
        raise FileNotFoundError("Face file not found.")

    image = face_recognition.load_image_file(full_name)
    new_encoding = face_recognition.face_encodings(image)[0]

    return new_encoding


def image_rotate(image, orientation=0):
    if orientation is None or orientation == 0:
        return image
    
    if orientation == 90:
        orientation = cv2.ROTATE_90_CLOCKWISE
    elif orientation == 180:
        orientation = cv2.ROTATE_180
    elif orientation == 270 or orientation == -90:
        orientation = cv2.ROTATE_90_COUNTERCLOCKWISE

    return cv2.rotate(image, orientation)


def image_resize(image, scale_factor):
    if scale_factor == 1.0:
        return image
    
    return cv2.resize(image, (0, 0), fx=scale_factor, fy=scale_factor, interpolation=cv2.INTER_AREA)


def save_image_to_cache(image, face_position=None, cache_folder=None, default_name="Unknown", 
                        face_encoding=None, known_face_encodings=None, face_names=None):
    
    if cache_folder is None:
        return default_name

    cache_folder = os.path.expanduser(cache_folder)

    if not os.path.isdir(cache_folder):
        try:
            os.makedirs(cache_folder, exist_ok=True)
        except Exception:
            logging.getLogger("IMG-SAVE").error("Unable to create cache folder")
            return default_name

    if face_position is None:
        top = 0
        right = image.shape[1]
        bottom = image.shape[0]
        left = 0
    else:
        top = face_position[0]
        right = face_position[1]
        bottom = face_position[2]
        left = face_position[3]

    cropped_im = image[top:bottom, left:right]
    cropped_im = cv2.cvtColor(cropped_im, cv2.COLOR_BGR2RGB)

    uid = uuid.uuid4()
    file_name = os.path.join(cache_folder, f"{uid}.jpg")
    face_name = get_next_cache_name(cache_folder=cache_folder, default_name=default_name)

    try:
        # self.addFace(file_name, face_name)

        cv2.imwrite(file_name, cropped_im)

        data = {}
        if os.path.isfile(os.path.join(cache_folder, "cache.yml")):
            data = kenzy.settings.load(os.path.join(cache_folder, "cache.yml"))

        data[face_name] = os.path.basename(file_name)

        kenzy.settings.save(data, os.path.join(cache_folder, "cache.yml"))

        if isinstance(known_face_encodings, list) and face_encoding is not None and isinstance(face_names, list) and face_name is not None:
            known_face_encodings.append(face_encoding)
            face_names.append(face_name)

    except Exception:
        if os.path.isfile(file_name):
            os.remove(file_name)

    return face_name


def get_next_cache_name(cache_folder, default_name="Unknown"):
    try:
        cache_settings = kenzy.settings.load(os.path.join(cache_folder, "cache.yml"))
    except Exception:
        cache_settings = {}

    seq = 1
    face_name = f"{default_name}-{seq}"

    while face_name in cache_settings:
        seq += 1
        face_name = f"{default_name}-{seq}"
    
    return face_name