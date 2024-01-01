from kenzy import __app_title__, __version__
import argparse
import logging
import cv2
import copy
from kenzy.extras import get_raw_value, apply_vars
from kenzy.image.core import motion_detection, image_blur, image_gray, image_markup, object_detection, \
    object_labels, object_model, face_detection, image_resize, image_rotate


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
        apply_vars(cfg, ARGS.set)

cfg["motion.detection"] = cfg.get("motion.detection", True)
cfg["object.detection"] = cfg.get("object.detection", True)
cfg["face.detection"] = cfg.get("face.detection", True)
cfg["face.recognition"] = cfg.get("face.recognition", True)

video_device = cv2.VideoCapture(get_raw_value(ARGS.video_device))
last_image = None

if cfg.get("object.detection"):
    model = object_model(model_type=cfg.get("object.model_type", "ssd"), 
                         model_config=cfg.get("object.model_config"), 
                         model_file=cfg.get("object.model_file"))

    labels = object_labels(label_file=cfg.get("object.label_file", None), model_type=cfg.get("object.model_type", None))

while True:
    ret, image = video_device.read()
    
    image = image_resize(image, get_raw_value(ARGS.scale))
    image = image_rotate(image, get_raw_value(ARGS.orientation))

    gray = image_blur(image_gray(image))

    if cfg.get("motion.detection"):
        movements = motion_detection(image=gray, last_image=last_image, 
                                     threshold=cfg.get("motion.threshold", 0.5), 
                                     motion_area=cfg.get("motion.motion_area", 0.0003))

        image_markup(image, elements=movements, line_color=cfg.get("motion.line_color", (0, 255, 0)))
    
    last_image = copy.copy(gray)

    if cfg.get("object.detection"):
        objects = object_detection(image=image, model=model, labels=labels, threshold=cfg.get("object.threshold", 0.5), 
                                   markup=cfg.get("object.markup", True), line_color=cfg.get("object.line_color", (255, 0, 0)), 
                                   font_color=cfg.get("object.font_color", (255, 255, 255)))

    if cfg.get("face.detection"):
        faces = face_detection(image=image, model=cfg.get("face.model", "hog"), 
                               face_encodings=None, face_names=None, 
                               tolerance=cfg.get("face.tolerance", 0.6), 
                               default_name=cfg.get("face.default_name"), 
                               markup=cfg.get("face.markup", True), 
                               line_color=cfg.get("face.line_color", (0, 0, 255)), 
                               font_color=cfg.get("face.font_color", (255, 255, 255)), 
                               cache_folder=None)

    cv2.imshow('KENZY_IMAGE', image)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

video_device.release()
cv2.destroyAllWindows()