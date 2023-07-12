import os
import logging
import ssl
from .extras import get_local_ip_address, update_defaults
from .core import KenzyHTTPServer, KenzyRequestHandler


def handle_startup_settings(args=None):
    if args.config is not None:
        if not os.path.isfile(args.config):
            raise FileNotFoundError("Configuration file not found.")
        
        config_file = args.config
    else:
        config_file = os.path.join(os.path.expanduser("~"), ".kenzy", "config.yml")

    #if not os.path.isfile(config_file):
    #    settings = DEFAULT_SETTINGS
    #else:       
    #    settings = load_settings(config_file)
    #    update_defaults(settings)

    #if str(args.type).lower() in ["core", "client"]:
    #    settings["type"] = str(args.type).lower()

    #return config_file, settings


def start_server(config_file=None, settings={}):
    
    http_settings = settings.get("http", {})
    host = http_settings.get("host", "0.0.0.0")
    port = http_settings.get("port", 8080)
    ssl_settings = http_settings.get("ssl", {})
    useSSL = ssl_settings.get("enable", False)
    ssl_cert_file = ssl_settings.get("cert_file")
    ssl_key_file = ssl_settings.get("key_file")

    if http_settings.get("url") is not None:
        service_url = http_settings.get("url")
    else:
        if host != "0.0.0.0":
            local_ip = host
        else:
            try:
                local_ip = get_local_ip_address()
            except Exception:
                local_ip = "127.0.0.1"

        proto = "http" if not useSSL else "https"
        
        service_url = "%s://%s:%s" % (proto, local_ip, port)
        http_settings["url"] = service_url
        
    # Create an HTTP server instance with the custom request handler
    httpd = KenzyHTTPServer((host, port), KenzyRequestHandler, config_file=config_file, settings=settings)

    try:
        # Wrap the HTTP server with SSL/TLS support
        if useSSL:
            httpd.socket = ssl.wrap_socket(httpd.socket, certfile=ssl_cert_file, keyfile=ssl_key_file, server_side=True)

        # Start the server
        httpd.serve_forever()

    except KeyboardInterrupt:
        try:
            httpd.shutdown()
        except Exception:
            pass
    except OSError as e:
        logging.error(f"Error starting the server: {str(e)}")
    except PermissionError as e:
        logging.error(f"Permission denied to bind to port {port}: {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
