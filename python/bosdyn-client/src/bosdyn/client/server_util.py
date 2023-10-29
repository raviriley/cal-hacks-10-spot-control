# Copyright (c) 2023 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""Helper functions and classes for creating and running a gRPC service."""

import copy
import logging
import signal
import time
from concurrent import futures

import grpc

import bosdyn.util
from bosdyn.api import (data_acquisition_store_pb2, data_buffer_pb2, header_pb2, image_pb2,
                        local_grid_pb2, log_annotation_pb2)
from bosdyn.client.channel import generate_channel_options

_LOGGER = logging.getLogger(__name__)


class ResponseContext(object):
    """Helper to log gRPC request and response message to the data buffer for a service.

    It should be called using a "with" statement each time an RPC is received such that
    the request and response proto messages can be passed in. It will automatically log
    the request and response to the data buffer, and mutates the headers to add additional
    information before logging.

    Args:
        response (protobuf): any gRPC response message with a bosdyn.api.ResponseHeader proto.
        request (protobuf): any gRPC request message with a bosdyn.api.RequestHeader proto.
        rpc_logger (DataBufferClient): Optional data buffer client to log the messages; if not
            provided, only the headers will be mutated and nothing will be logged.
        channel_prefix (string): the prefix you want this req / resp pair logged under.
        exc_callback (function): called with exception type, value, and traceback info if an
            exception is raised in the body of the "with" statement.
    """

    def __init__(self, response, request, rpc_logger=None, channel_prefix=None, exc_callback=None):
        self.response = response
        self.response.header.request_header.CopyFrom(request.header)
        self.request = request
        self.rpc_logger = rpc_logger
        self.channel_prefix = channel_prefix
        self.exc_callback = exc_callback

    def __enter__(self):
        """Adds a start timestamp to the response header and logs the request RPC."""
        self.response.header.request_received_timestamp.CopyFrom(bosdyn.util.now_timestamp())
        if self.rpc_logger:
            if self.channel_prefix is None:
                channel = None
            else:
                channel = self.channel_prefix + "/" + self.request.DESCRIPTOR.full_name
            self.rpc_logger.add_protobuf_async(self.request, channel)
        return self.response

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Updates the header code if unset and logs the response RPC."""
        if self.response.header.error.code == self.response.header.error.CODE_UNSPECIFIED:
            self.response.header.error.code = self.response.header.error.CODE_OK
        if exc_type is not None:
            # An uncaught exception was raised by the service. Automatically set the header
            # to be an internal error.
            self.response.header.error.code = self.response.header.error.CODE_INTERNAL_SERVER_ERROR
            self.response.header.error.message = "[%s] %s" % (exc_type.__name__, exc_val)
            if self.exc_callback:
                self.exc_callback(exc_type, exc_val, exc_tb)
        if self.rpc_logger:
            if self.channel_prefix is None:
                channel = None
            else:
                channel = self.channel_prefix + "/" + self.response.DESCRIPTOR.full_name
            self.rpc_logger.add_protobuf_async(self.response, channel)


class GrpcServiceRunner(object):
    """A runner to start a gRPC server on a background thread and allow easy cleanup.

    Args:
        service_servicer (custom servicer class derived from ServiceServicer): Servicer that
            defines server behavior.
        add_servicer_to_server_fn (function): Function generated by gRPC compilation that
            attaches the servicer to the gRPC server.
        port (int): The port number the service can be accessed through on the host system.
            Defaults to 0, which will assign an ephemeral port.
        max_send_message_length (int): Max message length (bytes) allowed for messages sent.
        max_receive_message_length (int): Max message length (bytes) allowed for messages received.
        timeout_secs (int): Number of seconds to wait for a clean server shutdown.
        force_sigint_capture (bool): Re-assign the SIGINT handler to default in order to prevent
            other scripts from blocking a clean exit. Defaults to True.
        logger (logging.Logger): Logger to log with.
    """

    def __init__(self, service_servicer, add_servicer_to_server_fn, port=0, max_workers=4,
                 max_send_message_length=None, max_receive_message_length=None, timeout_secs=3,
                 force_sigint_capture=True, logger=None):
        self.logger = logger or _LOGGER
        self.timeout_secs = timeout_secs
        self.force_sigint_capture = force_sigint_capture

        # Use the name of the service_servicer class for print messages.
        self.server_type_name = type(service_servicer).__name__

        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers),
            options=generate_channel_options(max_send_message_length, max_receive_message_length))
        add_servicer_to_server_fn(service_servicer, self.server)
        self.port = self.server.add_insecure_port('[::]:{}'.format(port))
        self.server.start()
        self.logger.info('Started the {} server.'.format(self.server_type_name))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def stop(self):
        """Blocks until the gRPC server shuts down."""
        self.logger.info("Shutting down the {} server.".format(self.server_type_name))
        shutdown_complete = self.server.stop(None)
        shutdown_complete.wait(self.timeout_secs)

    def run_until_interrupt(self):
        """Spin the thread until a SIGINT is received and then shut down cleanly."""
        if self.force_sigint_capture:
            # Ensure that KeyboardInterrupt is raised on a SIGINT.
            signal.signal(signal.SIGINT, signal.default_int_handler)

        # Monitor for SIGINT and shut down cleanly.
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        self.stop()


def populate_response_header(response, request, error_code=header_pb2.CommonError.CODE_OK,
                             error_msg=None):
    """Sets the ResponseHeader header in the response.
    Args:
        response (bosdyn.api Response message): The GRPC response message to be populated.
        request (bosdyn.api Request message): The header from the request is added to the response.
        error_code (header_pb2.CommonError): The status for the RPC response.
        error_msg (str): An optional error message describing a bad header status failure.
    Returns:
        Mutates the response message's header to be fully populated.
    """
    header = header_pb2.ResponseHeader()
    header.request_received_timestamp.CopyFrom(bosdyn.util.now_timestamp())
    header.request_header.CopyFrom(request.header)
    header.error.code = error_code
    if error_msg:
        header.error.message = error_msg
    copied_request = copy.copy(request)
    strip_large_bytes_fields(copied_request)
    header.request.Pack(copied_request)
    response.header.CopyFrom(header)


def strip_large_bytes_fields(proto_message):
    """Removes any large bytes fields from a protobuf message depending on the proto type."""
    message_type = type(proto_message)
    allowlist_map = get_bytes_field_allowlist()
    if message_type in allowlist_map:
        allowlist_map[message_type](proto_message)


def get_bytes_field_allowlist():
    """Creates set of protos which will have bytes fields removed."""
    allowlist_map = {
        image_pb2.GetImageResponse: strip_get_image_response,
        local_grid_pb2.GetLocalGridsResponse: strip_local_grid_responses,
        data_acquisition_store_pb2.StoreDataRequest: strip_store_data_request,
        data_acquisition_store_pb2.StoreImageRequest: strip_store_image_request,
        data_buffer_pb2.RecordSignalTicksRequest: strip_record_signal_tick,
        data_buffer_pb2.RecordDataBlobsRequest: strip_record_data_blob,
        log_annotation_pb2.AddLogAnnotationRequest: strip_log_annotation
    }
    return allowlist_map


def strip_image_response(proto_message):
    """Removes bytes from the image_pb2.ImageResponse proto."""
    proto_message.shot.image.ClearField("data")


def strip_get_image_response(proto_message):
    """Removes bytes from the image_pb2.GetImageResponse proto."""
    for img_resp in proto_message.image_responses:
        strip_image_response(img_resp)


def strip_local_grid_responses(proto_message):
    """Removes bytes from the local_grid_pb2.GetLocalGridsResponse proto."""
    for grid_resp in proto_message.local_grid_responses:
        grid_resp.local_grid.ClearField("data")


def strip_store_image_request(proto_message):
    """Removes bytes from the data_acquisition_store_pb2.StoreImageRequest proto."""
    proto_message.image.image.ClearField("data")


def strip_store_data_request(proto_message):
    """Removes bytes from the data_acquisition_store_pb2.StoreDataRequest proto."""
    proto_message.ClearField("data")


def strip_record_signal_tick(proto_message):
    """Removes bytes from the data_buffer_pb2.RecordSignalTicksRequest proto."""
    for tick_data in proto_message.tick_data:
        tick_data.ClearField("data")


def strip_record_data_blob(proto_message):
    """Removes bytes from the data_buffer_pb2.RecordDataBlobsRequest proto."""
    for blob in proto_message.blob_data:
        blob.ClearField("data")


def strip_log_annotation(proto_message):
    """Removes bytes from the log_annotation_pb2.AddLogAnnotationRequest proto."""
    for blob in proto_message.annotations.blob_data:
        blob.ClearField("data")
