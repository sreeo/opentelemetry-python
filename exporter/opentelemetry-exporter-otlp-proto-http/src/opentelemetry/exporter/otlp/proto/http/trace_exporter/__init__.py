# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from os import environ
from typing import Dict, Optional, Sequence
from time import sleep

import backoff
import requests

from opentelemetry.sdk.environment_variables import (
    OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE,
    OTEL_EXPORTER_OTLP_TRACES_COMPRESSION,
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
    OTEL_EXPORTER_OTLP_TRACES_HEADERS,
    OTEL_EXPORTER_OTLP_TRACES_TIMEOUT,
    OTEL_EXPORTER_OTLP_CERTIFICATE,
    OTEL_EXPORTER_OTLP_COMPRESSION,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_OTLP_HEADERS,
    OTEL_EXPORTER_OTLP_TIMEOUT,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.exporter.otlp.proto.http import (
    _OTLP_HTTP_HEADERS,
    Compression,
)
from opentelemetry.exporter.otlp.proto.http.exporter import  (
    OTLPExporterMixin, DEFAULT_COMPRESSION, DEFAULT_ENDPOINT, DEFAULT_TIMEOUT
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter.encoder import (
    _ProtobufEncoder,
)
from opentelemetry.util.re import parse_headers


_logger = logging.getLogger(__name__)

DEFAULT_TRACES_EXPORT_PATH = "v1/traces"

# Work around API change between backoff 1.x and 2.x. Since 2.0.0 the backoff
# wait generator API requires a first .send(None) before reading the backoff
# values from the generator.
_is_backoff_v2 = next(backoff.expo()) is None


def _expo(*args, **kwargs):
    gen = backoff.expo(*args, **kwargs)
    if _is_backoff_v2:
        gen.send(None)
    return gen


class OTLPSpanExporter(
    SpanExporter, OTLPExporterMixin[ReadableSpan, SpanExportResult]
):

    _MAX_RETRY_TIMEOUT = 64

    def __init__(
        self,
        endpoint: Optional[str] = None,
        certificate_file: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        compression: Optional[Compression] = None,
        session: Optional[requests.Session] = None,
    ):
        self._endpoint = endpoint or environ.get(
            OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
            _append_trace_path(
                environ.get(OTEL_EXPORTER_OTLP_ENDPOINT, DEFAULT_ENDPOINT)
            ),
        )
        self._certificate_file = certificate_file or environ.get(
            OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE,
            environ.get(OTEL_EXPORTER_OTLP_CERTIFICATE, True),
        )
        headers_string = environ.get(
            OTEL_EXPORTER_OTLP_TRACES_HEADERS,
            environ.get(OTEL_EXPORTER_OTLP_HEADERS, ""),
        )
        self._headers = headers or parse_headers(headers_string)
        self._timeout = timeout or int(
            environ.get(
                OTEL_EXPORTER_OTLP_TRACES_TIMEOUT,
                environ.get(OTEL_EXPORTER_OTLP_TIMEOUT, DEFAULT_TIMEOUT),
            )
        )
        self._compression = compression or _compression_from_env()
        self._session = session or requests.Session()
        self._session.headers.update(self._headers)
        self._session.headers.update(_OTLP_HTTP_HEADERS)
        if self._compression is not Compression.NoCompression:
            self._session.headers.update(
                {"Content-Encoding": self._compression.value}
            )
        self._shutdown = False

        OTLPExporterMixin.__init__(
            self,
            self._endpoint,
            self._certificate_file,
            self._headers,
            self._timeout,
            self._compression,
            self._session,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        # After the call to Shutdown subsequent calls to Export are
        # not allowed and should return a Failure result.
        if self._shutdown:
            _logger.warning("Exporter already shutdown, ignoring batch")
            return SpanExportResult.FAILURE

        serialized_data = _ProtobufEncoder.serialize(spans)

        for delay in _expo(max_value=self._MAX_RETRY_TIMEOUT):

            if delay == self._MAX_RETRY_TIMEOUT:
                return SpanExportResult.FAILURE

            resp = self._export(serialized_data)
            # pylint: disable=no-else-return
            if resp.status_code in (200, 202):
                return SpanExportResult.SUCCESS
            elif self._retryable(resp):
                _logger.warning(
                    "Transient error %s encountered while exporting span batch, retrying in %ss.",
                    resp.reason,
                    delay,
                )
                sleep(delay)
                continue
            else:
                _logger.error(
                    "Failed to export batch code: %s, reason: %s",
                    resp.status_code,
                    resp.text,
                )
                return SpanExportResult.FAILURE
        return SpanExportResult.FAILURE

    def shutdown(self):
        if self._shutdown:
            _logger.warning("Exporter already shutdown, ignoring call")
            return
        self._session.close()
        self._shutdown = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _compression_from_env() -> Compression:
    compression = (
        environ.get(
            OTEL_EXPORTER_OTLP_TRACES_COMPRESSION,
            environ.get(OTEL_EXPORTER_OTLP_COMPRESSION, "none"),
        )
        .lower()
        .strip()
    )
    return Compression(compression)


def _append_trace_path(endpoint: str) -> str:
    if endpoint.endswith("/"):
        return endpoint + DEFAULT_TRACES_EXPORT_PATH
    return endpoint + f"/{DEFAULT_TRACES_EXPORT_PATH}"
