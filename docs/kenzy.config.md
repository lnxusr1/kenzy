# Getting Started

Kenzy requires a configuration file to be provided which can start one or more device instances.  Each device can configured independently and enables the full set of configuration variables.

## Generic Example

The "type: multi" at the root level allows you to specify multiple child devices in a single configuration file.  The keyword ```default``` can be used to define global defaults that will apply to all services if not overridden in the specific device's configuration.

Here is a simple example:

```yaml
type: multi

default:
  device:
    location: Family Room
    group: Listener

  service:
    port: 9700

skillsmanager:
  type: kenzy.skillmanager

watcher:
  type: kenzy.image
  
listener:
  type: kenzy.stt

speaker:
  type: kenzy.tts

  device:
    model_target: cpu
```

Just save the configuration to a file ending in ".yml" and then start kenzy with:

```bash
kenzy --config /path/to/your/file.yml
```

-----

## Alternative Startup

Kenzy also enables individual calls so you can test out the various pieces while you work out exactly what you want your settings to be.  These additional options are installed as scripts and can be called as follows:

```bash
kenzy-image
kenzy-watch
kenzy-stt
kenzy-listen
kenzy-tts
kenzy-speak
```

If all else fails you can also call kenzy as a python model in any of the following ways:

```bash
python -m kenzy
python -m kenzy.image
python -m kenzy.stt
python -m kenzy.tts
```

Use the ```--help``` option with any of the above commands to get a full listing of parameters.

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)