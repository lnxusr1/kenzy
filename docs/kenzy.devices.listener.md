# KENZY: Speech-to-Text (STT)

The Speech-to-Text device does exactly what it's name suggests.  It translates captured audio into text phrases.  Spoken phrases are separated by idle "gap" time.  

The STT device also supports mute/unmute events which prevent it from capturing audio when it is set to "mute".  This is leveraged to keep Kenzy from listening to herself when she speaks.  Each "speak" command is preceded by a "mute" command and followed by an "unmute" command.  Since "speak" commands are processed sequentially this achieves the objective to avoid looping statements.

## Parameters
| Parameter                 | Type    | Default                | Description                          |
| :------------------------ | :------ | :--------------------- | :----------------------------------- |
| group                     | str     | *None*                 | Device membership group              |
| location                  | str     | *None*                 | Location, e.g. "Living Room"         |
| audio.device              | int     | *None*                 | PyAudio microphone device index      |
| audio.channels            | int     | 1                      | Audio channels for audio source      |
| audio.sample_rate         | int     | 16000                  | Audio sample rate of audio source    |
| speech.vad_aggressiveness | int     | 0                      | Voice activity detection  (0 thru 3) |
| speech.buffer_padding     | int     | 350                    | Speech gap time in milliseconds      |
| speech.buffer_size        | int     | 50                     | Buffer size for speech frames        |
| speech.ratio              | float   | 0.75                   | Must be decimal between 0 and 1      |
| speech.model              | str     | openai/whisper-tiny.en | Path or name of Whisper Model        |
| offline                   | bool    | false                  | Disables downloading the models      |

Note:  You should consider only setting ```offline``` after you have executed the program at least once so that it fully downloads all model files.  Once they are downloaded you can switch the offline mode on so that it does not try to re-download the models (which enables the program to then run without an Internet connection).

## Example YAML File

See [Service Settings](kenzy.containers.md) for options in the *service* group.

```yaml
type: kenzy.stt

device: 
  location:                  Living Room
  group:                     Primary
  audio.device:              0
  audio.channels:            1
  audio.sample_rate:         16000
  speech.vad_aggressiveness: 0
  speech.buffer_padding:     350
  speech.buffer_size:        50
  speech.ratio:              0.75
  speech.model:              openai/whisper-tiny.en
  offline:                   false

service:
  host:                      0.0.0.0
  port:                      9701
```
-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)