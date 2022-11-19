# kenzy.devices.Watcher

## Parameters:
| Parameter            | Type    | Default | Description |
| :------------------- | :------ | :------ | :---------- |
| framesPerSecond      | float   | 29.97   | Frames per second.  Defaults to NTSC. |
| orientation          | int     | 0       | Device orientation. (0, 90, 180, or 270) |
| videoDeviceIndex     | int     | 0       | Video device index number in OpenCV |
| classifierFile       | str     | -       | (Optional) Default is Haarcascade Frontalface |
| recognizerFile       | str     | -       | (Optional) Trained faces file in YML format |
| namesFile            | str     | -       | (Optional) Friendly names file in JSON format |
| trainingSourceFolder | str     | -       | (Optional) Source folder for training routine |
| nickname             | str     | None    | (Optional) Nickname of the device |

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)