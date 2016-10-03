__author__ = 'breddels'
import numpy as np
import logging
import threading
import uuid
import time
import ast

try:
	import __builtin__
except ImportError:
	import builtins as __builtin__

from .dataset import Dataset, Subspace, Task, _issequence
import vaex.promise
import vaex.settings
import vaex.utils
try:
	import Cookie  # py2
except ImportError:
	import http.cookies as Cookie  # py3


from tornado.httpclient import AsyncHTTPClient, HTTPClient
import tornado.httputil
import tornado.websocket
from tornado.concurrent import Future
from tornado import gen
from .dataset import default_shape

logger = logging.getLogger("vaex.remote")

DEFAULT_REQUEST_TIMEOUT = 60 * 5 # 5 minutes

def wrap_future_with_promise(future):
	if isinstance(future, vaex.promise.Promise): # TODO: not so nice, sometimes we pass a promise
		return future
	promise = vaex.promise.Promise()
	def callback(future):
		#print(("callback", future, future.result()))
		e = future.exception()
		if e:
			#print(("reject", e))
			promise.reject(e)
		else:
			promise.fulfill(future.result())
	future.add_done_callback(callback)
	return promise

import tornado.ioloop

import threading
import json

def listify(value):
	if isinstance(value, list):
		value = list([listify(item) for item in value])
	if hasattr(value, "tolist"):
		value = value.tolist()
	return value


try:
	from urllib.request import urlopen
	from urllib.parse import urlparse, urlencode
except ImportError:
	from urlparse import urlparse
	from urllib import urlopen, urlencode

import threading

def _check_error(object):
	if "error" in object:
		raise RuntimeError("Server responded with error: %r" % object["error"])


class ServerRest(object):
	def __init__(self, hostname, port=5000, base_path="/", background=False, thread_mover=None, websocket=True):
		self.hostname = hostname
		self.port = port
		self.base_path = base_path if base_path.endswith("/") else (base_path + "/")
		#if async:
		event = threading.Event()
		self.thread_mover = thread_mover or (lambda fn, *args, **kwargs: fn(*args, **kwargs))
		logger.debug("thread mover: %r", self.thread_mover)

		# jobs maps from uid to tasks
		self.jobs = {}

		def ioloop_threaded():
			logger.debug("creating tornado io_loop")
			self.io_loop = tornado.ioloop.IOLoop().current()
			event.set()
			logger.debug("started tornado io_loop...")

			self.io_loop.start()
			self.io_loop.close()
			logger.debug("stopped tornado io_loop")

		io_loop = tornado.ioloop.IOLoop.current(instance=False)
		if True:# io_loop:# is None:
			logger.debug("no current io loop, starting it in thread")
			thread = threading.Thread(target=ioloop_threaded)
			thread.setDaemon(True)
			thread.start()
			event.wait()
		else:
			logger.debug("using current io loop")
			self.io_loop = io_loop

		self.io_loop.make_current()
		#if async:
		self.http_client_async = AsyncHTTPClient()
		self.http_client = HTTPClient()
		self.user_id = vaex.settings.webclient.get("cookie.user_id")
		self.use_websocket = websocket
		self.websocket = None

		self.submit = self.submit_http

		if self.use_websocket:
			logger.debug("connect via websocket")
			self.submit = self.submit_websocket
			self._websocket_connect()
			logger.debug("websocket connected")

	def close(self):
		#self.http_client_async.
		self.http_client.close()
		if self.use_websocket:
			if self.websocket:
					self.websocket.close()
		self.io_loop.stop()


	def _websocket_connect(self):
		def connected(websocket):
			logger.debug("connected to websocket: %s" % self._build_url(""))
		def failed(reason):
			logger.error("failed to connect to %s" % self._build_url(""))
		self.websocket_connected = vaex.promise.Promise()
		self.websocket_connected.then(connected, failed)
		if 0:
			connected = wrap_future_with_promise(tornado.websocket.websocket_connect(self._build_url("websocket"), on_message_callback=self._on_websocket_message))
			connected.get()
			self.websocket_connected.fulfill(connected)
		def do():
			try:
				logger.debug("wrapping promise")
				connected = wrap_future_with_promise(tornado.websocket.websocket_connect(self._build_url("websocket"), on_message_callback=self._on_websocket_message))
				logger.debug("continue")
				self.websocket_connected.fulfill(connected)
			except:
				logger.exception("error connecting")
				#raise
		logger.debug("add callback")
		self.io_loop.add_callback(do)
		logger.debug("added callback: ")
		#self.io_loop.start()
		#if self.port == 29345:
		#	import pdb
		#	pdb.set_trace()
		logger.debug("waiting for connection")
		result = self.websocket_connected.get()
		logger.debug("websocket connected")
		if self.websocket_connected.isRejected:
			raise self.websocket.reason




	def _on_websocket_message(self, msg):
		json_data, data = msg.split(b"\n", 1)
		response = json.loads(json_data.decode("utf8"))
		if data:
			numpy_array = np.fromstring(data, dtype=np.dtype(response["dtype"])).reshape(ast.literal_eval(response["shape"]))
			response["result"] = numpy_array
		import sys
		if sys.getsizeof(msg) > 1024*4:
			logger.debug("socket read message: <large amount of data>",)
		else:
			logger.debug("socket read message: %s", msg)
			logger.debug("json response: %r", response)
		# for the moment, job == task, in the future a job can be multiple tasks
		job_id = response.get("job_id")
		if job_id:
			try:
				phase = response["job_phase"]
				logger.debug("job update %r, phase=%r", job_id, phase)
				if phase == "COMPLETED":
					result = response["result"]#[0]
					#logger.debug("completed job %r, result=%r", job_id, result)
					task = self.jobs[job_id]
					logger.debug("completed job %r (async=%r, thread_mover=%r)", job_id, task.async, self.thread_mover)
					processed_result = task.post_process(result)
					if task.async:
						self.thread_mover(task.fulfill, processed_result)
					else:
						task.fulfill(processed_result)
				elif phase == "EXCEPTION":
					logger.error("exception happened at server side: %r", response)
					class_name = response["exception"]["class"]
					msg = response["exception"]["msg"]
					exception = getattr(__builtin__, class_name)(msg)
					logger.debug("error in job %r, exception=%r", job_id, exception)
					task = self.jobs[job_id]
					if task.async:
						self.thread_mover(task.reject, exception)
					else:
						task.reject(exception)
				elif phase == "ERROR":
					logger.error("error happened at server side: %r", response)
					msg = response["error"]
					exception = RuntimeError("error at server: %r" % msg)
					task = self.jobs[job_id]
					if task.async:
						self.thread_mover(task.reject, exception)
					else:
						task.reject(exception)
				elif phase == "PENDING":
					fraction = response["progress"]
					logger.debug("pending?: %r", phase)
					task = self.jobs[job_id]
					if task.async:
						self.thread_mover(task.signal_progress.emit, fraction)
					else:
						task.signal_progress.emit(fraction)
			except Exception as e:
				logger.exception("error in handling job", e)
				task = self.jobs[job_id]
				if task.async:
					self.thread_mover(task.reject, e)
				else:
					task.reject(e)


	def wait(self):
		io_loop = tornado.ioloop.IOLoop.instance()
		io_loop.start()

	def submit_websocket(self, path, arguments, async=False, post_process=lambda x: x):
		assert self.use_websocket

		task = TaskServer(post_process=post_process, async=async)
		logger.debug("created task: %r, %r (async=%r)" % (path, arguments, async))
		job_id = str(uuid.uuid4())
		self.jobs[job_id] = task
		arguments["job_id"] = job_id
		arguments["path"] = path
		arguments["user_id"] = self.user_id
		#arguments = dict({key: (value.tolist() if hasattr(value, "tolist") else value) for key, value in arguments.items()})
		arguments = dict({key: listify(value) for key, value in arguments.items()})

		def do():
			def write(socket):
				try:
					logger.debug("write to websocket: %r", arguments)
					socket.write_message(json.dumps(arguments))
					return
				except:
					import traceback
					traceback.print_exc()
			#return
			logger.debug("will schedule a write to the websocket")
			self.websocket_connected.then(write).end()#.then(task.fulfill)

		self.io_loop.add_callback(do)
		logger.debug("we can continue (main thread is %r)", threading.currentThread())
		if async:
			return task
		else:
			return task.get()

	def submit_http(self, path, arguments, post_process, async, **kwargs):
		def pre_post_process(response):
			cookie = Cookie.SimpleCookie()
			for cookieset in response.headers.get_list("Set-Cookie"):
				cookie.load(cookieset)
				logger.debug("cookie load: %r", cookieset)
			logger.debug("cookie: %r", cookie)
			if "user_id" in cookie:
				user_id = cookie["user_id"].value
				logger.debug("user_id: %s", user_id)
				if self.user_id != user_id:
					self.user_id = user_id
					vaex.settings.webclient.store("cookie.user_id", self.user_id)
			data = response.body
			is_json = False
			logger.info("response is: %r", response.body)
			logger.info("content_type is: %r", response.headers["Content-Type"])
			if response.headers["Content-Type"] == "application/numpy-array":
				text = response.body.decode("ascii")
				shape, dtype, data = text.split("\n", 3)
				import ast
				numpy_array = np.fromstring(data, dtype=np.dtype(dtype)).reshape(ast.literal_eval(shape))
				return post_process(numpy_array)
			else:
				try:
					data = json.loads(response.body.decode("ascii"))
					is_json = True
				except Exception as e:
					logger.info("couldn't convert to json (error is %s, assume it's raw data): %s", e, data)
					#logger.info("couldn't convert to json (error is %s, assume it's raw data)", e)
				if is_json:
					self._check_exception(data)
					return post_process(data["result"])
				else:
					return post_process(data)

		arguments = {key: listify(value) for key, value in arguments.items()}
		import pdb
		arguments_json = {key:json.dumps(value) for key, value in arguments.items()}
		headers = tornado.httputil.HTTPHeaders()

		url = self._build_url(path +"?" + urlencode(arguments_json))
		logger.debug("fetch %s, async=%r", url, async)

		if self.user_id is not None:
			headers.add("Cookie", "user_id=%s" % self.user_id)
			logger.debug("adding user_id %s to request", self.user_id)
		if async:
			task = TaskServer(pre_post_process, async=async)
			# tornado doesn't like that we call fetch while ioloop is running in another thread, we should use ioloop.add_callbacl
			def do():
				self.thread_mover(task.signal_progress.emit, 0.5)
				future = self.http_client_async.fetch(url, headers=headers, request_timeout=DEFAULT_REQUEST_TIMEOUT, **kwargs)
				def thread_save_succes(value):
					self.thread_mover(task.signal_progress.emit, 1.0)
					self.thread_mover(task.fulfill, value)
				def thread_save_failure(value):
					self.thread_mover(task.reject, value)
				wrap_future_with_promise(future).then(pre_post_process).then(thread_save_succes, thread_save_failure)
			self.io_loop.add_callback(do)
			return task #promise.then(self._move_to_thread)
		else:
			return pre_post_process(self.http_client.fetch(url, headers=headers, request_timeout=DEFAULT_REQUEST_TIMEOUT, **kwargs))

	def datasets(self, as_dict=False, async=False):
		def post(result):
			logger.debug("datasets result: %r", result)
			datasets = [DatasetRest(self, **kwargs) for kwargs in result]
			logger.debug("datasets: %r", datasets)
			return datasets if not as_dict else dict([(ds.name, ds) for ds in datasets])
		return self.submit(path="datasets", arguments={}, post_process=post, async=async)

	def _build_url(self, method):
		protocol = "ws" if self.use_websocket else "http"
		return "%s://%s:%d%s%s" % (protocol, self.hostname, self.port, self.base_path, method)

	def _call_subspace(self, method_name, subspace, **kwargs):
		def post_process(result):
			if method_name == "histogram": # histogram is the exception..
				# TODO: don't do binary transfer, just json, now we cannot handle exception
				import base64
				#logger.debug("result: %r", result)
				#result = base64.b64decode(result) #.decode("base64")
				#result = base64.
				data = np.fromstring(result, dtype=np.float64)
				shape = (kwargs["size"],) * subspace.dimension
				data = data.reshape(shape)
				return data
			else:
				try:
					return np.array(result)
				except ValueError:
					return result
		dataset_name = subspace.dataset.name
		expressions = subspace.expressions
		async = subspace.async
		path = "datasets/%s/%s" % (dataset_name, method_name)
		url = self._build_url(path)
		arguments = dict(kwargs)
		if not subspace.dataset.get_auto_fraction():
			arguments["active_fraction"] = subspace.dataset.get_active_fraction()
			arguments["active_start_index"], arguments["active_end_index"] = subspace.dataset.get_active_range()
		selection = subspace.get_selection()
		if selection is not None:
			arguments["selection"] = selection.to_dict()
		arguments["variables"] = list(subspace.dataset.variables.items())
		arguments["virtual_columns"] = list(subspace.dataset.virtual_columns.items())
		arguments.update(dict(expressions=expressions))
		return self.submit(path, arguments, post_process=post_process, async=async)

	def _call_dataset(self, method_name, dataset_remote, async, numpy=False, **kwargs):
		def post_process(result):
			#result = self._check_exception(json.loads(result.body))["result"]
			if numpy:
				return np.fromstring(result, dtype=np.float64)
			else:
				try:
					return np.array(result)
				except ValueError:
					return result
		path = "datasets/%s/%s" % (dataset_remote.name, method_name)
		arguments = dict(kwargs)
		if not dataset_remote.get_auto_fraction():
			arguments["active_fraction"] = dataset_remote.get_active_fraction()
		arguments["variables"] = list(dataset_remote.variables.items())
		arguments["virtual_columns"] = list(dataset_remote.virtual_columns.items())
		selections = {}
		for name in dataset_remote.selection_histories.keys():
			selection = dataset_remote.get_selection(name)
			if selection:
				selection = selection.to_dict()
			selections[name] = selection
		arguments["selections"] = selections
		#arguments["selection_name"] = json.dumps(dataset_remote.get_selection_name())
		body = urlencode(arguments)

		return self.submit(path, arguments, post_process=post_process, async=async)
		#return self.fetch(url+"?"+body, wrap, async=async, method="GET")
		#return self._return(result, wrap)

	def _schedule_call(self, method_name, dataset_remote, async, **kwargs):
		def post_process(result):
			#result = self._check_exception(json.loads(result.body))["result"]
			try:
				return np.array(result)
			except ValueError:
				return result
		method = "%s/%s" % (dataset_remote.name, method_name)
		#arguments = dict(kwargs)
		#if not dataset_remote.get_auto_fraction():
		#	arguments["active_fraction"] = dataset_remote.get_active_fraction()
		#arguments["variables"] = list(dataset_remote.variables.items())
		#arguments["virtual_columns"] = list(dataset_remote.virtual_columns.items())
		#arguments["selection_name"] = json.dumps(dataset_remote.get_selection_name())
		#body = urlencode(arguments)

		return self.schedule(path, arguments, post_process=post_process, async=async)

	def _check_exception(self, reply_json):
		if "exception" in reply_json:
			logger.error("exception happened at server side: %r", reply_json)
			class_name = reply_json["exception"]["class"]
			msg = reply_json["exception"]["msg"]
			raise getattr(__builtin__, class_name)(msg)
		if "error" in reply_json:
			raise ValueError("unknown error occured at server")
		else:
			return reply_json

class SubspaceRemote(Subspace):
	def toarray(self, list):
		return np.array(list)

	@property
	def dimension(self):
		return len(self.expressions)

	def _task(self, promise):
		"""Helper function for returning tasks results, result when immediate is True, otherwise the task itself, which is a promise"""
		if self.async:
			return promise
		else:
			return promise

	def sleep(self, seconds, async=False):
		return self.dataset.server.call("sleep", seconds, async=async)

	def minmax(self):
		return self._task(self.dataset.server._call_subspace("minmax", self))
		#return self._task(task)

	def histogram(self, limits, size=256, weight=None):
		return self._task(self.dataset.server._call_subspace("histogram", self, size=size, limits=limits, weight=weight))

	def nearest(self, point, metric=None):
		point = vaex.utils.make_list(point)
		result = self.dataset.server._call_subspace("nearest", self, point=point, metric=metric)
		return self._task(result)

	def mean(self):
		return self.dataset.server._call_subspace("mean", self)

	def correlation(self, means=None, vars=None):
		return self.dataset.server._call_subspace("correlation", self, means=means, vars=vars)

	def var(self, means=None):
		return self.dataset.server._call_subspace("var", self, means=means)

	def sum(self):
		return self.dataset.server._call_subspace("sum", self)

	def limits_sigma(self, sigmas=3, square=False):
		return self.dataset.server._call_subspace("limits_sigma", self, sigmas=sigmas, square=square)

	def mutual_information(self, limits=None, size=256):
		return self.dataset.server._call_subspace("mutual_information", self, limits=limits, size=size)

class DatasetRemote(Dataset):
	def __init__(self, name, server, column_names):
		super(DatasetRemote, self).__init__(name, column_names)
		self.server = server

import astropy.units

class DatasetRest(DatasetRemote):
	def __init__(self, server, name, column_names, dtypes, ucds, descriptions, units, description, full_length, virtual_columns=None):
		DatasetRemote.__init__(self, name, server.hostname, column_names)
		self.server = server
		self.name = name
		self.column_names = column_names
		#self.dtypes = {name: np.zeros(1, dtype=getattr(np, dtype)).dtype for name, dtype in dtypes.items()}
		self.dtypes = {name: np.dtype(dtype) for name, dtype in dtypes.items()}
		self.units = {name: astropy.units.Unit(unit) for name, unit in units.items()}
		self.virtual_columns.update(virtual_columns or {})
		self.ucds = ucds
		self.descriptions = descriptions
		self.description = description
		self._full_length = full_length
		self._length = full_length
		self._index_end = self._length
		#self.filename = #"http://%s:%s/%s" % (server.hostname, server.port, name)
		self.path = self.filename = self.server._build_url("%s" % name)


		self.fraction = 1

		self.executor = ServerExecutor()

	def count(self, expression=None, binby=[], limits=None, shape=default_shape, selection=False, async=False):
		return self._async(async, self.server._call_dataset("count", self, async=True, expression=expression, binby=binby, limits=limits, shape=shape, selection=selection))

	def mean (self, expression, binby=[], limits=None, shape=default_shape, selection=False, async=False):
		return self._async(async, self.server._call_dataset("mean", self, async=True, expression=expression, binby=binby, limits=limits, shape=shape, selection=selection))

	def sum(self, expression, binby=[], limits=None, shape=default_shape, selection=False, async=False):
		return self._async(async, self.server._call_dataset("sum", self, async=True, expression=expression, binby=binby, limits=limits, shape=shape, selection=selection))

	def var(self, expression, binby=[], limits=None, shape=default_shape, selection=False, async=False):
		return self._async(async, self.server._call_dataset("var", self, async=True, expression=expression, binby=binby, limits=limits, shape=shape, selection=selection))

	def minmax(self, expression, binby=[], limits=None, shape=default_shape, selection=False, async=False):
		return self._async(async, self.server._call_dataset("minmax", self, async=True, expression=expression, binby=binby, limits=limits, shape=shape, selection=selection))

	#def count(self, expression=None, binby=[], limits=None, shape=default_shape, selection=False, async=False):
	def cov        (self, x, y=None, binby=[], limits=None, shape=default_shape, selection=False, async=False):
		return self._async(async, self.server._call_dataset("cov", self, async=True, x=x, y=y, binby=binby, limits=limits, shape=shape, selection=selection))

	def correlation(self, x, y=None, binby=[], limits=None, shape=default_shape, sort=False, sort_key=np.abs, selection=False, async=False):
		# TODO: sort and sort_key should be done locally
		return self._async(async, self.server._call_dataset("correlation", self, async=True, x=x, y=y, binby=binby, limits=limits, shape=shape, selection=selection))

	def _async(self, async, task, progressbar=False):
		if async:
			return task
		else:
			result = task.get()
			logger.debug("result = %r", result)
			return result

	def dtype(self, expression):
		if expression in self.dtypes:
			return self.dtypes[expression]
		else:
			return np.zeros(1, dtype=np.float64).dtype

	def is_local(self): return False

	def __repr__(self):
		name = self.__class__.__module__ + "." +self.__class__.__name__
		return "<%s(server=%r, name=%r, column_names=%r, __len__=%r)> instance at 0x%x" % (name, self.server, self.name, self.column_names, len(self), id(self))

	def __call__(self, *expressions, **kwargs):
		return SubspaceRemote(self, expressions, kwargs.get("executor") or self.executor, async=kwargs.get("async", False))

	def evaluate(self, expression, i1=None, i2=None, out=None, async=False):
		result = self.server._call_dataset("evaluate", self, expression=expression, i1=i1, i2=i2, async=async)
		# TODO: we ignore out
		return result


# we may get rid of this when we group together tasks
class ServerExecutor(object):
	def __init__(self):
		self.signal_begin = vaex.events.Signal("begin")
		self.signal_progress = vaex.events.Signal("progress")
		self.signal_end = vaex.events.Signal("end")
		self.signal_cancel = vaex.events.Signal("cancel")

	def execute(self):
		logger.debug("dummy execute")

from vaex.dataset import Task
class TaskServer(Task):
	def __init__(self, post_process, async):
		vaex.dataset.Task.__init__(self, None, [])
		self.post_process = post_process
		self.async = async
		self.task_queue = []

	def schedule(self, task):
		self.task_queue.append(task)
		logger.info("task added, queue: %r", self.task_queue)
		return task


	def execute(self):
		logger.debug("starting with execute")
		if self._is_executing:
			logger.debug("nested execute call")
			# this situation may happen since in this methods, via a callback (to update a progressbar) we enter
			# Qt's eventloop, which may execute code that will call execute again
			# as long as that code is using async tasks (i.e. promises) we can simple return here, since after
			# the execute is almost finished, any new tasks added to the task_queue will get executing
			return
		# u 'column' is uniquely identified by a tuple of (dataset, expression)
		self._is_executing = True
		try:
			t0 = time.time()
			task_queue_all = list(self.task_queue)
			if not task_queue_all:
				logger.info("only had cancelled tasks")
			logger.info("clearing queue")
			#self.task_queue = [] # Ok, this was stupid.. in the meantime there may have been new tasks, instead, remove the ones we copied
			for task in task_queue_all:
				logger.info("remove from queue: %r", task)
				self.task_queue.remove(task)
			logger.info("left in queue: %r", self.task_queue)
			task_queue_all = [task for task in task_queue_all if not task.cancelled]
			logger.debug("executing queue: %r" % (task_queue_all))

			#for task in self.task_queue:
			#$	print task, task.expressions_all
			datasets = set(task.dataset for task in task_queue_all)
			cancelled = [False]
			def cancel():
				logger.debug("cancelling")
				self.signal_cancel.emit()
				cancelled[0] = True
			try:
				# process tasks per dataset
				self.signal_begin.emit()
				for dataset in datasets:
					task_queue = [task for task in task_queue_all if task.dataset == dataset]
					expressions = list(set(expression for task in task_queue for expression in task.expressions_all))

					for task in task_queue:
						task._results = []
						task.signal_progress.emit(0)
					self.server.execute_queue(task_queue)
					self._is_executing = False
			except:
				# on any error we flush the task queue
				self.signal_cancel.emit()
				logger.exception("error in task, flush task queue")
				raise
			logger.debug("executing took %r seconds" % (time.time() - t0))
			# while processing the self.task_queue, new elements will be added to it, so copy it
			logger.debug("cancelled: %r", cancelled)
			if cancelled[0]:
				logger.debug("execution aborted")
				task_queue = task_queue_all
				for task in task_queue:
					#task._result = task.reduce(task._results)
					#task.reject(UserAbort("cancelled"))
					# remove references
					task._result = None
					task._results = None
			else:
				task_queue = task_queue_all
				for task in task_queue:
					logger.debug("fulfill task: %r", task)
					if not task.cancelled:
						task._result = task.reduce(task._results)
						task.fulfill(task._result)
						# remove references
					task._result = None
					task._results = None
				self.signal_end.emit()
				# if new tasks were added as a result of this, execute them immediately
				# TODO: we may want to include infinite recursion protection
				self._is_executing = False
				if len(self.task_queue) > 0:
					logger.debug("task queue not empty.. start over!")
					self.execute()
		finally:
			self._is_executing = False

if __name__ == "__main__":
	import vaex
	import sys
	vaex.set_log_level_debug()
	server = vaex.server(sys.argv[1], port=int(sys.argv[2]))
	datasets = server.datasets()
	print(datasets)
	dataset = datasets[0]
	dataset = vaex.example()
	print(dataset("x").minmax())
	dataset.select("x < 0")
	print(dataset.selected_length(), len(dataset))
	print(dataset("x").selected().is_masked)
	print(dataset("x").selected().minmax())
