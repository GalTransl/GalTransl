"""Canceled browser requests stop JSON writes without attempting a second response."""

import errno
from http import HTTPStatus
import tempfile
import unittest
from unittest.mock import Mock, patch

from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class JsonDisconnectTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(build_handler(JobRegistry()))
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.close_connection = False
        return handler

    def test_dictionary_route_does_not_send_error_after_client_disconnect(self):
        for failure in (ConnectionAbortedError(10053, 'client aborted'),
                        ConnectionResetError(10054, 'client reset'), BrokenPipeError()):
            for stage in ('headers', 'body'):
                with self.subTest(failure=type(failure).__name__, stage=stage):
                    handler = self.handler()
                    handler.command = 'POST'
                    handler._read_json_body = Mock(return_value={'entries': [{'pre_src': '猫'}]})
                    if stage == 'headers':
                        handler.end_headers.side_effect = failure
                    else:
                        handler.wfile.write.side_effect = failure
                    with tempfile.TemporaryDirectory() as project:
                        with patch('GalTransl.CacheDictionary.inspect_cache_dictionaries', return_value={'results': []}):
                            handler._route_project_api(encode_project_dir(project), '/cache/dictionary-matches')
                    handler.send_response.assert_called_once_with(HTTPStatus.OK)
                    self.assertTrue(handler.close_connection)
                    if stage == 'headers':
                        handler.wfile.write.assert_not_called()
                    else:
                        handler.wfile.write.assert_called_once()

    def test_unrelated_write_errors_are_not_hidden(self):
        handler = self.handler()
        handler.wfile.write.side_effect = OSError(errno.EIO, 'write failure')
        with self.assertRaises(OSError):
            handler._send_json({'ok': True})
        self.assertFalse(handler.close_connection)

    def test_successful_response_is_unchanged(self):
        handler = self.handler()
        handler._send_json({'text': '你好'})
        handler.send_response.assert_called_once_with(HTTPStatus.OK)
        handler.wfile.write.assert_called_once_with('{"text": "你好"}'.encode('utf-8'))
        self.assertFalse(handler.close_connection)


if __name__ == '__main__':
    unittest.main()
