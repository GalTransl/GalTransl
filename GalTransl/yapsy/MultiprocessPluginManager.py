# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t; python-indent: 4 -*-

"""
Role
====

Defines a plugin manager that runs all plugins in separate process
linked by pipes.


API
===
"""

import multiprocessing as mproc

from GalTransl.yapsy.IMultiprocessPlugin import IMultiprocessPlugin
from GalTransl.yapsy.IMultiprocessChildPlugin import IMultiprocessChildPlugin
from GalTransl.yapsy.MultiprocessPluginProxy import MultiprocessPluginProxy
from GalTransl.yapsy.PluginManager import  PluginManager


class MultiprocessPluginManager(PluginManager):
	"""
	Subclass of the PluginManager that runs each plugin in a different process
	"""	

	def __init__(self,
				 categories_filter=None,
				 directories_list=None,
				 plugin_info_ext=None,
				 plugin_locator=None):
		if categories_filter is None:
			categories_filter = {"Default": IMultiprocessPlugin}
		PluginManager.__init__(self,
								 categories_filter=categories_filter,
								 directories_list=directories_list,
								 plugin_info_ext=plugin_info_ext,
								 plugin_locator=plugin_locator)
		self.connections = []

		
	def instanciateElementWithImportInfo(self, element, element_name,
										 plugin_module_name, candidate_filepath):
		"""This method instanciates each plugin in a new process and links it to
		the parent with a pipe.

		In the parent process context, the plugin's class is replaced by
		the ``MultiprocessPluginProxy`` class that hold the information
		about the child process and the pipe to communicate with it.

		.. warning:: 
		    The plugin code should only use the pipe to
			communicate with the rest of the applica`tion and should not
			assume any kind of shared memory, not any specific functionality
			of the `multiprocessing.Process` parent class (its behaviour is
			different between platforms !)
		
		See ``IMultiprocessPlugin``.
		"""
		if element is IMultiprocessChildPlugin:
			# The following will keep retro compatibility for IMultiprocessChildPlugin
			raise Exception("Preventing instanciation of a bar child plugin interface.")
		instanciated_element = MultiprocessPluginProxy()
		parent_pipe, child_pipe = mproc.Pipe()
		instanciated_element.child_pipe = parent_pipe
		instanciated_element.proc = MultiprocessPluginManager._PluginProcessWrapper(
			element_name, plugin_module_name, candidate_filepath,
			child_pipe)
		instanciated_element.proc.start()
		return instanciated_element


	class _PluginProcessWrapper(mproc.Process):
		"""Helper class that strictly needed to be able to spawn the
		plugin on Windows but kept also for Unix platform to get a more
		uniform behaviour.

		This will handle re-importing the plugin's module in the child
		process (again this is necessary on windows because what has
		been imported in the main thread/process will not be shared with
		the spawned process.)
		"""
		def __init__(self, element_name, plugin_module_name, candidate_filepath, child_pipe):
			self.element_name = element_name
			self.child_pipe = child_pipe
			self.plugin_module_name = plugin_module_name
			self.candidate_filepath = candidate_filepath
			mproc.Process.__init__(self)
	 
		def run(self):
			module = PluginManager._importModule(self.plugin_module_name,
												 self.candidate_filepath)
			element = getattr(module, self.element_name)
			e = element(self.child_pipe)
			e.run()
