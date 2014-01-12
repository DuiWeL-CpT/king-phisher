#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  king_phisher/server/server.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the  nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import json
import logging
import os
import random
import shutil
import sqlite3
import string
import threading

from AdvancedHTTPServer import *
from AdvancedHTTPServer import build_server_from_config
from AdvancedHTTPServer import SectionConfigParser

from king_phisher import job
from king_phisher import sms
from king_phisher import xor
from king_phisher.server import authenticator
from king_phisher.server import rpcmixin

__version__ = '0.0.1'
DEFAULT_PAGE_PATH = '/usr/share:/usr/local/share:data/server:.'

make_uid = lambda: ''.join(random.choice(string.ascii_letters + string.digits) for x in range(24))

def which_page(page):
	is_readable = lambda ppath: (os.path.isfile(ppath) and os.access(ppath, os.R_OK))
	for path in DEFAULT_PAGE_PATH.split(os.pathsep):
		path = path.strip('"')
		page_file = os.path.join(path, 'king_phisher', page)
		if is_readable(page_file):
			return page_file
	return None

def build_king_phisher_server(config, section_name):
	# set config defaults
	config.set(section_name, 'tracking_image', 'email_logo_banner.gif')
	config.set(section_name, 'secret_id', make_uid())

	king_phisher_server = build_server_from_config(config, 'server', ServerClass = KingPhisherServer, HandlerClass = KingPhisherRequestHandler)
	king_phisher_server.http_server.config = SectionConfigParser('server', config)
	return king_phisher_server

class KingPhisherRequestHandler(rpcmixin.KingPhisherRequestHandlerRPCMixin, AdvancedHTTPServerRequestHandler):
	def install_handlers(self):
		super(KingPhisherRequestHandler, self).install_handlers()
		self.database = self.server.database
		self.database_lock = threading.RLock()
		self.config = self.server.config
		self.handler_map['^kpdd$'] = self.handle_deaddrop_visit

		tracking_image = self.config.get('tracking_image')
		tracking_image = tracking_image.replace('.', '\\.')
		self.handler_map['^' + tracking_image + '$'] = self.handle_email_opened

	def issue_alert(self, alert_text, campaign_id = None):
		campaign_name = None
		with self.get_cursor() as cursor:
			if campaign_id:
				cursor.execute('SELECT name FROM campaigns WHERE id = ?', (campaign_id,))
				campaign_name = cursor.fetchone()[0]
				cursor.execute('SELECT user_id FROM alert_subscriptions WHERE campaign_id = ?', (campaign_id,))
			else:
				cursor.execute('SELECT id FROM users WHERE phone_number IS NOT NULL AND phone_carrier IS NOT NULL')
			user_ids = map(lambda user_id: user_id[0], cursor.fetchall())
		if campaign_name != None and '{campaign_name}' in alert_text:
			alert_text = alert_text.format(campaign_name = campaign_name)
		for user_id in user_ids:
			with self.get_cursor() as cursor:
				cursor.execute('SELECT phone_number, phone_carrier FROM users WHERE id = ?', (user_id,))
				number, carrier = cursor.fetchone()
			self.server.logger.debug("sending alert SMS message to {0} ({1})".format(number, carrier))
			sms.send_sms(alert_text, number, carrier, 'donotreply@kingphisher.local')

	def do_GET(self, *args, **kwargs):
		self.server.throttle_semaphore.acquire()
		try:
			super(KingPhisherRequestHandler, self).do_GET(*args, **kwargs)
		except:
			raise
		finally:
			self.server.throttle_semaphore.release()

	def do_POST(self, *args, **kwargs):
		self.server.throttle_semaphore.acquire()
		try:
			super(KingPhisherRequestHandler, self).do_POST(*args, **kwargs)
		except:
			raise
		finally:
			self.server.throttle_semaphore.release()

	def do_RPC(self, *args, **kwargs):
		self.server.throttle_semaphore.acquire()
		try:
			super(KingPhisherRequestHandler, self).do_RPC(*args, **kwargs)
		except:
			raise
		finally:
			self.server.throttle_semaphore.release()

	def get_query_parameter(self, parameter):
		return self.query_data.get(parameter, [None])[0]

	def custom_authentication(self, username, password):
		return self.server.forked_authenticator.authenticate(username, password)

	def check_authorization(self):
		# don't require authentication for GET & POST requests
		if self.command in ['GET', 'POST']:
			return True
		# deny anything not GET or POST if it's not from 127.0.0.1
		if self.client_address[0] != '127.0.0.1':
			return False
		return super(KingPhisherRequestHandler, self).check_authorization()

	@property
	def campaign_id(self):
		if hasattr(self, '_campaign_id'):
			return self._campaign_id
		self._campaign_id = None
		if self.message_id:
			with self.get_cursor() as cursor:
				cursor.execute('SELECT campaign_id FROM messages WHERE id = ?', (self.message_id,))
				result = cursor.fetchone()
			if result:
				self._campaign_id = result[0]
		return self._campaign_id

	@property
	def message_id(self):
		if hasattr(self, '_message_id'):
			return self._message_id
		msg_id = self.get_query_parameter('id')
		if not msg_id:
			kp_cookie_name = self.config.get('cookie_name', 'KPID')
			if kp_cookie_name in self.cookies:
				visit_id = self.cookies[kp_cookie_name].value
				with self.get_cursor() as cursor:
					cursor.execute('SELECT message_id FROM visits WHERE id = ?', (visit_id,))
					result = cursor.fetchone()
				if result:
					msg_id = result[0]
		self._message_id = msg_id
		return self._message_id

	@property
	def vhost(self):
		return self.headers.get('Host')

	def respond_file(self, file_path, attachment = False, query = {}):
		file_path = os.path.abspath(file_path)
		file_ext = os.path.splitext(file_path)[1].lstrip('.')
		try:
			file_obj = open(file_path, 'rb')
		except IOError:
			self.respond_not_found()
			return None

		if self.config.getboolean('require_id') and self.message_id != self.config.get('secret_id'):
			# a valid campaign_id requires a valid message_id
			if not self.campaign_id:
				self.server.logger.warning('denying request with not found due to lack of id')
				self.respond_not_found()
				return None
			if self.query_count('SELECT COUNT(id) FROM landing_pages WHERE campaign_id = ? AND hostname = ?', (self.campaign_id, self.vhost)) == 0:
				self.server.logger.warning('denying request with not found due to invalid hostname')
				self.respond_not_found()
				return None

		self.send_response(200)
		self.send_header('Content-Type', self.guess_mime_type(file_path))
		fs = os.fstat(file_obj.fileno())
		self.send_header('Content-Length', str(fs[6]))
		if attachment:
			file_name = os.path.basename(file_path)
			self.send_header('Content-Disposition', 'attachment; filename=' + file_name)
		self.send_header('Last-Modified', self.date_time_string(fs.st_mtime))

		if file_ext in ['', 'htm', 'html']:
			try:
				self.handle_page_visit()
			except Exception as err:
				# TODO: log execeptions here
				pass

		self.end_headers()
		shutil.copyfileobj(file_obj, self.wfile)
		file_obj.close()
		return

	def respond_not_found(self):
		self.send_response(404, 'Resource Not Found')
		self.send_header('Content-Type', 'text/html')
		self.end_headers()
		page_404 = which_page('error_404.html')
		if page_404:
			shutil.copyfileobj(open(page_404), self.wfile)
		else:
			self.wfile.write('Resource Not Found\n')
		return

	def handle_deaddrop_visit(self, query):
		data = query['token'][0]
		data = data.decode('base64')
		data = xor.xor_decode(data)
		data = json.loads(data)

		deployment_id = data.get('deaddrop_id')
		with self.get_cursor() as cursor:
			cursor.execute('SELECT campaign_id FROM deaddrop_deployments WHERE id = ?', (deployment_id,))
			campaign_id = cursor.fetchone()
			if not campaign_id:
				self.send_response(200)
				self.end_headers()
				return
			campaign_id = campaign_id[0]

		local_username = data.get('local_username')
		local_hostname = data.get('local_hostname')
		if campaign_id == None or local_username == None or local_hostname == None:
			return
		local_ip_addresses = data.get('local_ip_addresses')
		if isinstance(local_ip_addresses, (list, tuple)):
			local_ip_addresses = ' '.join(local_ip_addresses)

		with self.get_cursor() as cursor:
			cursor.execute('SELECT id FROM deaddrop_connections WHERE deployment_id = ? AND local_username = ? AND local_hostname = ?', (deployment_id, local_username, local_hostname))
			drop_id = cursor.fetchone()
			if drop_id:
				drop_id = drop_id[0]
				cursor.execute('UPDATE deaddrop_connections SET visit_count = visit_count + 1, last_visit = CURRENT_TIMESTAMP WHERE id = ?', (drop_id,))
				self.send_response(200)
				self.end_headers()
				return
			values = (deployment_id, campaign_id, self.client_address[0], local_username, local_hostname, local_ip_addresses)
			cursor.execute('INSERT INTO deaddrop_connections (deployment_id, campaign_id, visitor_ip, local_username, local_hostname, local_ip_addresses) VALUES (?, ?, ?, ?, ?, ?)', values)
		self.send_response(200)
		self.end_headers()
		return

	def handle_email_opened(self, query):
		# image size: 49 Bytes
		img_data  = '47494638396101000100910000000000ffffffffffff00000021f90401000002'
		img_data += '002c00000000010001000002025401003b'
		img_data  = img_data.decode('hex')
		self.send_response(200)
		self.send_header('Content-Type', 'image/gif')
		self.send_header('Content-Length', str(len(img_data)))
		self.end_headers()
		self.wfile.write(img_data)

		msg_id = self.get_query_parameter('id')
		if not msg_id:
			return
		with self.get_cursor() as cursor:
			cursor.execute('UPDATE messages SET opened = CURRENT_TIMESTAMP WHERE id = ? AND opened IS NULL', (msg_id,))

	def handle_page_visit(self):
		if not self.message_id:
			return
		if not self.campaign_id:
			return
		message_id = self.message_id
		campaign_id = self.campaign_id
		with self.get_cursor() as cursor:
			# set the opened timestamp to the visit time if it's null
			cursor.execute('UPDATE messages SET opened = CURRENT_TIMESTAMP WHERE id = ? AND opened IS NULL', (self.message_id,))

		kp_cookie_name = self.config.get('cookie_name', 'KPID')
		if not kp_cookie_name in self.cookies:
			visit_id = make_uid()
			cookie = "{0}={1}; Path=/; HttpOnly".format(kp_cookie_name, visit_id)
			self.send_header('Set-Cookie', cookie)
			with self.get_cursor() as cursor:
				client_ip = self.client_address[0]
				user_agent = (self.headers.getheader('user-agent') or '')
				cursor.execute('INSERT INTO visits (id, message_id, campaign_id, visitor_ip, visitor_details) VALUES (?, ?, ?, ?, ?)', (visit_id, message_id, campaign_id, client_ip, user_agent))
				visit_count = self.query_count('SELECT COUNT(id) FROM visits WHERE campaign_id = ?', (campaign_id,))
			if visit_count > 0 and ((visit_count in [1, 10, 25]) or ((visit_count % 50) == 0)):
				alert_text = "{0} vists reached for campaign: {{campaign_name}}".format(visit_count)
				self.server.job_manager.job_run(self.issue_alert, (alert_text, campaign_id))
		else:
			visit_id = self.cookies[kp_cookie_name].value
			if self.query_count('SELECT COUNT(id) FROM landing_pages WHERE campaign_id = ? AND hostname = ? AND page = ?', (self.campaign_id, self.vhost, self.path)):
				with self.get_cursor() as cursor:
					cursor.execute('UPDATE visits SET visit_count = visit_count + 1, last_visit = CURRENT_TIMESTAMP WHERE id = ?', (visit_id,))

		username = None
		for pname in ['username', 'user', 'u']:
			username = (self.get_query_parameter(pname) or self.get_query_parameter(pname.title()) or self.get_query_parameter(pname.upper()))
			if username:
				break
		if username:
			password = None
			for pname in ['password', 'pass', 'p']:
				password = (self.get_query_parameter(pname) or self.get_query_parameter(pname.title()) or self.get_query_parameter(pname.upper()))
				if password:
					break
			password = (password or '')
			cred_count = 0
			with self.get_cursor() as cursor:
				cursor.execute('SELECT COUNT(id) FROM credentials WHERE message_id = ? AND username = ? AND password = ?', (message_id, username, password))
				if cursor.fetchone()[0] == 0:
					cursor.execute('INSERT INTO credentials (visit_id, message_id, campaign_id, username, password) VALUES (?, ?, ?, ?, ?)', (visit_id, message_id, campaign_id, username, password))
					cred_count = self.query_count('SELECT COUNT(id) FROM credentials WHERE campaign_id = ?', (campaign_id,))
			if cred_count > 0 and ((cred_count in [1, 5, 10]) or ((cred_count % 25) == 0)):
				alert_text = "{0} credentials submitted for campaign: {{campaign_name}}".format(cred_count)
				self.server.job_manager.job_run(self.issue_alert, (alert_text, campaign_id))

class KingPhisherServer(AdvancedHTTPServer):
	def __init__(self, *args, **kwargs):
		super(KingPhisherServer, self).__init__(*args, **kwargs)
		self.database = None
		self.logger = logging.getLogger('KingPhisher.Server')
		self.serve_files = True
		self.serve_files_list_directories = False
		self.serve_robots_txt = True
		self.http_server.forked_authenticator = authenticator.ForkedAuthenticator()
		self.logger.debug('forked an authenticating process with PID: ' + str(self.http_server.forked_authenticator.child_pid))
		self.http_server.throttle_semaphore = threading.Semaphore()
		self.http_server.job_manager = job.JobManager()
		self.http_server.job_manager.start()

	def load_database(self, database_file):
		if database_file == ':memory:':
			db = database.create_database(database_file)
		else:
			db = sqlite3.connect(database_file, check_same_thread = False)
		self.database = db
		self.http_server.database = db

	def shutdown(self, *args, **kwargs):
		self.logger.warning('processing shutdown request')
		super(KingPhisherServer, self).shutdown(*args, **kwargs)
		self.http_server.forked_authenticator.stop()
		self.logger.debug('stopped the forked authenticator process')
		self.http_server.job_manager.stop()
