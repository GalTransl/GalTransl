# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t; python-indent: 4 -*-


"""
Role
====

Originally defined the basic interfaces for multiprocessed plugins.

Deprecation Note
================

This class is deprecated and replaced by :doc:`IMultiprocessChildPlugin`.

Child classes of `IMultiprocessChildPlugin` used to be an `IPlugin` as well as
a `multiprocessing.Process`, possibly playing with the functionalities of both,
which make maintenance harder than necessary.

And indeed following a bug fix to make multiprocess plugins work on Windows,
instances of IMultiprocessChildPlugin inherit Process but are not exactly the
running process (there is a new wrapper process).

API
===
"""

from multiprocessing import Process
from GalTransl.yapsy.IMultiprocessPlugin import IMultiprocessPlugin


class IMultiprocessChildPlugin(IMultiprocessPlugin, Process):
	"""
	Base class for multiprocessed plugin.

	DEPRECATED(>1.11): Please use IMultiProcessPluginBase instead !
	"""

	def __init__(self, parent_pipe):
		IMultiprocessPlugin.__init__(self, parent_pipe)
		Process.__init__(self)

	def run(self):
		"""
		Override this method in your implementation
		"""
		return
