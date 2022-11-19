# kenzy.devices.Listener

## Parameters
| Parameter           | Type    | Default | Description |
| :------------------ | :------ | :------ | :---------- |
| audioChannels       | int     | 1       | Audio channels for audio source |
| audioSampleRate     | int     | 16000   | Audio sample rate of audio source |
| vadAggressiveness   | int     | 1       | Noise filtering level.  Accepts 1 thru 3. |
| speechRatio         | float   | 0.75    | Must be between 0 and 1 as a decimal |
| speechBufferSize    | int     | 50      | Buffer size for speech frames |
| speechBufferPadding | int     | 350     | Padding, in milliseconds, of speech frames |
| audioDeviceIndex    | int     | 0       | Microphone device accourding to PyAudio |
| speechModel         | str     | None    | Path and filename of Coqui Speech Model file |
| speechScorer        | str     | None    | (Optional) Path and filename of Coqui Scorer file |
| nickname            | str     | None    | (Optional) Nickname of the device |

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)