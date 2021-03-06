#!/usr/bin/env python

import sys
import io
import os
import shutil
from subprocess import Popen, PIPE
from string import Template
from struct import Struct
from threading import Thread, Lock
from time import sleep, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from wsgiref.simple_server import make_server
from wsgiref.handlers import SimpleHandler
from ws4py.compat import get_connection
from urllib.parse import urlparse, parse_qs

import picamera
from ws4py.websocket import WebSocket
from ws4py.server.wsgirefserver import WSGIServer, WebSocketWSGIRequestHandler, WebSocketWSGIHandler
from ws4py.server.wsgiutils import WebSocketWSGIApplication

import explorerhat as eh

###########################################
# CONFIGURATION
WIDTH = 640
HEIGHT = 480
FRAMERATE = 24
HTTP_PORT = 8082
WS_PORT = 8084
COLOR = u'#444'
BGCOLOR = u'#333'
JSMPEG_MAGIC = b'jsmp'
JSMPEG_HEADER = Struct('>4sHH')
###########################################


class StreamingHttpHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
            return
        elif url.path == '/move':
            try:
                data = {k: v[0] for k, v in parse_qs(url.query).items()}
            except (IndexError, ValueError) as e:
                print("there was an error")
                self.send_error(400, str(e))

            else:
                with self.server.hat_lock:
                    if 'direction' in data:
                        direction = data['direction']
                        if direction == 'forwards':
                            eh.motor.one.forwards()
                            eh.motor.two.forwards()
                        elif direction == 'backwards':
                            eh.motor.one.backwards()
                            eh.motor.two.backwards()
                        elif direction == 'left':
                            eh.motor.one.backwards()
                            eh.motor.two.forwards()
                        elif direction == 'right':
                            eh.motor.two.backwards()
                            eh.motor.one.forwards()
                        self.server.last_move = time()
                self.send_response(200)
                self.end_headers()
            return
        elif url.path == '/stop':
            eh.motor.one.stop()
            eh.motor.two.stop()
            self.send_response(200)
            self.end_headers()
            return
        elif self.path == '/jsmpg.js':
            content_type = 'application/javascript'
            content = self.server.jsmpg_content
        elif self.path == '/index.html':
            content_type = 'text/html; charset=utf-8'
            tpl = Template(self.server.index_template)
            content = tpl.safe_substitute(dict(
                ADDRESS=WS_PORT,
                WIDTH=WIDTH, HEIGHT=HEIGHT, COLOR=COLOR, BGCOLOR=BGCOLOR))
        else:
            self.send_error(404, 'File not found')
            return
        content = content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(content))
        self.send_header('Last-Modified', self.date_time_string(time()))
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(content)


def MotorHandler(self):
    def __init__(self):
        print("this is a test")

    def run(self):
        while True:
            nowtime = time()
            if nowtime - self.server.last_move > 2000:
                with self.server.hat_lock:
                    eh.motor.one.stop()
                    eh.motor.two.stop()


class StreamingHttpServer(HTTPServer):
    def __init__(self):
        super(StreamingHttpServer, self).__init__(
                ('', HTTP_PORT), StreamingHttpHandler)
        self.hat_lock = Lock()
        self.last_move = 0
        motor_handler = MotorHandler(self.hat_lock)
        motor_thread = Thread(target=motor_handler)
        motor_thread.start()
        with io.open('index.html', 'r') as f:
            self.index_template = f.read()
        with io.open('jsmpg.js', 'r') as f:
            self.jsmpg_content = f.read()


class StreamingWebSocket(WebSocket):
    def opened(self):
        self.send(JSMPEG_HEADER.pack(JSMPEG_MAGIC, WIDTH, HEIGHT), binary=True)


class BroadcastOutput(object):
    def __init__(self, camera):
        print('Spawning background conversion process')
        self.converter = Popen([
            'avconv',
            '-f', 'rawvideo',
            '-pix_fmt', 'yuv420p',
            '-s', '%dx%d' % camera.resolution,
            '-r', str(float(camera.framerate)),
            '-i', '-',
            '-f', 'mpeg1video',
            '-b', '800k',
            '-r', str(float(camera.framerate)),
            '-'],
            stdin=PIPE, stdout=PIPE, stderr=io.open(os.devnull, 'wb'),
            shell=False, close_fds=True)

    def write(self, b):
        self.converter.stdin.write(b)

    def flush(self):
        print('Waiting for background conversion process to exit')
        self.converter.stdin.close()
        self.converter.wait()


class BroadcastThread(Thread):
    def __init__(self, converter, websocket_server):
        super(BroadcastThread, self).__init__()
        self.converter = converter
        self.websocket_server = websocket_server

    def run(self):
        try:
            while True:
                buf = self.converter.stdout.read(512)
                if buf:
                    self.websocket_server.manager.broadcast(buf, binary=True)
                elif self.converter.poll() is not None:
                    break
        finally:
            self.converter.stdout.close()


class JasonsWebSocketWSGIHandler(WebSocketWSGIHandler):
    def setup_environ(self):
        """
        Setup the environ dictionary and add the
        `'ws4py.socket'` key. Its associated value
        is the real socket underlying socket.
        """
        SimpleHandler.setup_environ(self)
        self.environ['ws4py.socket'] = get_connection(self.environ['wsgi.input'])
        self.http_version = 1.1

class JasonsWebSocketRequestHandler(WebSocketWSGIRequestHandler):
    def handle(self):
        """
        Unfortunately the base class forces us
        to override the whole method to actually provide our wsgi handler.
        """
        self.raw_requestline = self.rfile.readline()
        if not self.parse_request(): # An error code has been sent, just exit
            return

        # next line is where we'd have expect a configuration key somehow
        handler = JasonsWebSocketWSGIHandler(
            self.rfile, self.wfile, self.get_stderr(), self.get_environ()
        )
        handler.request_handler = self      # backpointer for logging
        handler.run(self.server.get_app())

def main():
    print('Initializing camera')
    with picamera.PiCamera() as camera:
        camera.rotation = 180
        camera.resolution = (WIDTH, HEIGHT)
        camera.framerate = FRAMERATE
        sleep(1) # camera warm-up time
        print('Initializing websockets server on port %d' % WS_PORT)
        websocket_server = make_server(
            '', WS_PORT,
            server_class=WSGIServer,
            handler_class=JasonsWebSocketRequestHandler,
            app=WebSocketWSGIApplication(handler_cls=StreamingWebSocket))
        websocket_server.initialize_websockets_manager()
        websocket_thread = Thread(target=websocket_server.serve_forever)
        print('Initializing HTTP server on port %d' % HTTP_PORT)
        http_server = StreamingHttpServer()
        http_thread = Thread(target=http_server.serve_forever)
        print('Initializing broadcast thread')
        output = BroadcastOutput(camera)
        broadcast_thread = BroadcastThread(output.converter, websocket_server)
        print('Starting recording')
        camera.start_recording(output, 'yuv')
        try:
            print('Starting websockets thread')
            websocket_thread.start()
            print('Starting HTTP server thread')
            http_thread.start()
            print('Starting broadcast thread')
            broadcast_thread.start()
            while True:
                camera.wait_recording(1)
        except KeyboardInterrupt:
            pass
        finally:
            print('Stopping recording')
            camera.stop_recording()
            print('Waiting for broadcast thread to finish')
            broadcast_thread.join()
            print('Shutting down HTTP server')
            http_server.shutdown()
            print('Shutting down websockets server')
            websocket_server.shutdown()
            print('Waiting for HTTP server thread to finish')
            http_thread.join()
            print('Waiting for websockets thread to finish')
            websocket_thread.join()
            print('Stopping motors')
            eh.motor.one.stop()
            eh.motor.two.stop()


if __name__ == '__main__':
    main()
