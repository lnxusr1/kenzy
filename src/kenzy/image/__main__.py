from kenzy import __app_title__, __version__
import argparse
import logging
import cv2
import copy
import os
import json
from kenzy.image.core import motion_detection, image_blur, image_gray, image_markup, object_detection, \
    object_labels, object_model, face_detection, image_resize, image_rotate


def get_raw_value(setting_value):
    if "." in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
        setting_value = float(setting_value.replace(",", ""))
    elif "." not in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
        setting_value = int(setting_value.replace(",", ""))
    elif setting_value.lower().strip() in ["true", "false"]:
        setting_value = bool(setting_value.lower().strip())
    elif setting_value.startswith("{") and setting_value.endswith("}"):
        setting_value = json.loads(setting_value)
    elif setting_value.startswith("[") and setting_value.endswith("]"):
        setting_value = json.loads(setting_value)

    return setting_value


parser = argparse.ArgumentParser(
    description=__app_title__ + " " + __version__,
    formatter_class=argparse.RawTextHelpFormatter,
    epilog='''For more information visit:\nhttp://kenzy.ai''')

parser.add_argument('-v', '--version', action="store_true", help="Print Version")
parser.add_argument('-d', '--video-device', default="0", help="Video Device")
parser.add_argument('-o', '--orientation', default="0", help="Image orientation (0, 90, 180, 270)")
parser.add_argument('--scale', default="1.0", help="Scale percentage (as decimal)")
parser.add_argument('-s', '--set', action="append", help="Override settings as: name=value")

logging_group = parser.add_argument_group('Logging Options')

logging_group.add_argument('--log-level', default="info", help="Options are full, debug, info, warning, error, and critical")
logging_group.add_argument('--log-file', default=None, help="Redirects all logging messages to the specified file")

ARGS = parser.parse_args()

# VERSION 
if ARGS.version:
    print(__app_title__, __version__)
    quit()

# LOG LEVEL

logLevel = logging.INFO
if ARGS.log_level is not None and ARGS.log_level.strip().lower() in ["debug", "info", "warning", "error", "critical"]:
    logLevel = eval("logging." + ARGS.log_level.strip().upper())
elif ARGS.log_level is not None and ARGS.log_level.strip().lower() == "full":
    logLevel = logging.DEBUG

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    datefmt='%Y-%m-%d %H:%M:%S %z',
    filename=ARGS.log_file,
    format='%(asctime)s %(name)-12s - %(levelname)-9s - %(message)s',
    level=logLevel)


# CORE FUNCTION

cfg = { }

if ARGS.set is not None:
    if isinstance(ARGS.set, list):
        for item in ARGS.set:
            if "=" in item:
                setting_name, setting_value = item.split("=", 1)
                cfg[setting_name] = get_raw_value(setting_value)
            else:
                logging.critical("Invalid setting provided.  Must be in form: name=value")
                quit(1)

video_device = cv2.VideoCapture(get_raw_value(ARGS.video_device))
last_image = None

model = object_model(model_type=cfg.get("object.model_type", "ssd"), 
                     model_config=cfg.get("object.model_config"), 
                     model_file=cfg.get("object.model_file"))

labels = object_labels(label_file=cfg.get("object.label_file", None), model_type=cfg.get("object.model_type", None))

while True:
    ret, image = video_device.read()
    
    image = image_resize(image, get_raw_value(ARGS.scale))
    image = image_rotate(image, get_raw_value(ARGS.orientation))

    gray = image_blur(image_gray(image))

    movements = motion_detection(image=gray, last_image=last_image, 
                                 threshold=cfg.get("motion.threshold", 0.5), 
                                 motion_area=cfg.get("motion.motion_area", 0.0003))
    
    last_image = copy.copy(gray)
    
    objects = object_detection(image=image, model=model, labels=labels, threshold=cfg.get("object.threshold", 0.5), 
                               markup=cfg.get("object.markup", True), line_color=cfg.get("object.line_color", (255, 0, 0)), 
                               font_color=cfg.get("object.font_color", (255, 255, 255)))

    faces = face_detection(image=image, model=cfg.get("face.model", "hog"), 
                           face_encodings=None, face_names=None, 
                           tolerance=cfg.get("face.tolerance", 0.6), 
                           default_name=cfg.get("face.default_name"), 
                           markup=cfg.get("face.marketup", True), 
                           line_color=cfg.get("face.line_color", (0, 0, 255)), 
                           font_color=cfg.get("face.font_color", (255, 255, 255)), 
                           cache_folder=None)

    image_markup(image, elements=movements, line_color=cfg.get("motion.line_color", (0, 255, 0)))

    cv2.imshow('KENZY_IMAGE', image)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

video_device.release()
cv2.destroyAllWindows()