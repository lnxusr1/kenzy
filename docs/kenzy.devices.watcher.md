# kenzy.devices.Watcher

| Parameter           | Type    | Default | Description                                    |
| :------------------ | :------ | :------ | :--------------------------------------------- |
| group               | str     | *None*  | Device membership group                        |
| location            | str     | *None*  | Location, e.g. "Living Room"                   |
| video_device        | int     | 0       | OpenCV Video Device Index                      |
| scale               | float   | 1.0     | Image scaling coefficient (0.5 = 50% size)     |
| frames_per_second   | float   | None    | Video FPS.  Auto-calculated if left blank      |
| orientation         | int     | 0       | Device orientation. (0, 90, 180, or 270)       |
| motion.detection    | bool    | True    | Enables/disables motion detection              |
| motion.threshold    | int     | 20      | Threshold of pixel color change                |
| motion.area         | float   | 0.0003  | Percentage of pixels changed to trigger motion |
| object.detection    | bool    | True    | Enables/disables object detection              |
| object.threshold    | float   | 0.6     | Confidence score for object detection          |
| object.model_type   | str     | ssd     | Object detection type (ssd or yolo)            |
| object.model_config | str     | None    | Model configuration file                       |
| object.model_file   | str     | None    | Model file (.pb or .pt)                        |
| objects.label_file  | str     | None    | Object labels list                             |
| face.detection      | bool    | True    | Enables/disables face detection                |
| face.recognition    | bool    | True    | Enables/disables face recognition              |
| face.tolerance      | float   | 0.5     | Euclidean distance (smaller is more accurate)  |
| face.default_name   | str     | Unknown | Default name for face if not recognized        |
| face.cache_folder   | str     | None    | Cache folder for faces identified              |
| face.entries        | dict    | None    | Dictionary of face names with examples         |
| record.enabled      | bool    | True    | Enables/disables video recording               |
| record.format       | str     | XVID    | Video output format for saved recordings       |
| record.folder       | str     | None    | Folder for saved recordings                    |
| record.buffer       | int     | 5       | Seconds to buffer pre/post detection           |

## Face Entries

```face.entries``` provides a way to supply face image samples along with a name to associate with those samples.  At least one image per name must be supplied.

Here's an example:
```
  face.entries:
    lnxusr1:
      - ~/Pictures/faces/lnxusr1/user.1.8.jpg
      - ~/Pictures/faces/lnxusr1/user.1.9.jpg
    "Jon Doe":
      - ~/Pictures/faces/jon_doe/IMG_8991.jpg
      - ~/Pictures/faces/jon_doe/IMG_9991.jpg
    "Jane Doe":
      - ~/Pictures/faces/jane_doe/IMG_1423.jpg
      - ~/Pictures/faces/jane_doe/IMG_5336.jpg
```

## Example YAML File

```
type: kenzy.image

device: 
  location:                 Living Room
  group:                    Primary
  video_device:             0
  orientation:              0
  frames_per_second:        30
  motion.detection:         True
  motion.threshold:         20
  motion.area:              0.0003
  object.detection:         True
  object.threshold:         0.6
  object.model_type:        ssd
  face.detection:           True
  face.recognition:         True
  face.tolerance:           0.5
  face.cache_folder:        ~/.kenzy/image/faces/unknown
  face.default_name:        Unknown
  record.enabled:           true
  record.format:            XVID
  record.buffer:            5
  record.folder:            ~/Videos/kenzy
  face.entries:
    lnxusr1:
      - ~/Pictures/faces/lnxusr1/user.1.9.jpg

service:
  host:                     0.0.0.0
  port:                     9703
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)