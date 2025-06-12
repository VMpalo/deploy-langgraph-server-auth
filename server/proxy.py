"""
LangGraph Proxy Middleware

This module handles forwarding requests to the LangGraph server,
including support for streaming responses and error handling.
"""

import logging
from typing import Dict, Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse, StreamingResponse
from starlette.requests import Request

from server.health import handle_health_check

logger = logging.getLogger(__name__)


class LangGraphProxyMiddleware(BaseHTTPMiddleware):
    """
    Middleware that forwards authenticated requests to the LangGraph server.
    
    Handles both streaming and non-streaming requests, with proper error
    handling and health check routing.
    """
    
    def __init__(self, app, langgraph_url: str):
        super().__init__(app)
        self.langgraph_url = langgraph_url
        self.client = httpx.AsyncClient(timeout=300.0)  # 5 minute timeout for long requests
        logger.info(f"LangGraph proxy middleware initialized for {langgraph_url}")
    
    async def dispatch(self, request: Request, call_next) -> Response:
        """
        Forward the request to LangGraph server.

        Note: call_next is not used because this middleware terminates the chain
        by forwarding requests to the external LangGraph server.
        """
        
        # Handle health checks locally (don't forward to LangGraph)
        if request.url.path in ["/ok", "/health", "/health-detailed"]:
            return await handle_health_check(request, self.langgraph_url)
        
        # Forward all other requests to LangGraph server
        try:
            return await self._forward_request(request)
            
        except httpx.ConnectError:
            logger.error(f"Failed to connect to LangGraph server at {self.langgraph_url}")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "LangGraph server unavailable",
                    "detail": f"Could not connect to internal LangGraph server at {self.langgraph_url}"
                }
            )
        except Exception as e:
            logger.error(f"Error forwarding request to LangGraph server: {e}")
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Proxy error",
                    "detail": str(e)
                }
            )
    
    async def _forward_request(self, request: Request) -> Response:
        """
        Forward a request to the LangGraph server.
        
        Args:
            request: The incoming request to forward
            
        Returns:
            Response: The response from LangGraph server
        """
        # Build the target URL
        target_url = f"{self.langgraph_url}{request.url.path}"
        if request.url.query:
            target_url += f"?{request.url.query}"

        # Get request body if present
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            body = await request.body()

        # Forward headers (excluding host)
        headers = self._prepare_headers(request)

        # Check if this is a streaming request
        is_streaming = self._is_streaming_request(request)

        if is_streaming:
            logger.info(f"Detected streaming request: {request.method} {request.url.path}")
            return await self._handle_streaming_request(request.method, target_url, headers, body)
        else:
            return await self._handle_regular_request(request.method, target_url, headers, body)

    def _prepare_headers(self, request: Request) -> Dict[str, str]:
        """
        Prepare headers for forwarding, removing problematic ones.
        
        Args:
            request: The incoming request
            
        Returns:
            Dict[str, str]: Cleaned headers for forwarding
        """
        headers = dict(request.headers)
        headers.pop("host", None)  # Remove host header to avoid conflicts
        return headers

    def _is_streaming_request(self, request: Request) -> bool:
        """
        Determine if this is a streaming request.
        
        Args:
            request: The incoming request
            
        Returns:
            bool: True if this appears to be a streaming request
        """
        return (
            request.url.path.endswith("/stream") or
            "/runs/stream" in request.url.path or
            request.headers.get("accept") == "text/event-stream"
        )

    async def _handle_streaming_request(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> StreamingResponse:
        """
        Handle a streaming request to LangGraph server.
        
        Args:
            method: HTTP method
            url: Target URL
            headers: Request headers
            body: Request body
            
        Returns:
            StreamingResponse: Streaming response from LangGraph
        """
        logger.debug(f"Handling streaming request to {url}")

        # Start the streaming request
        stream_request = self.client.stream(
            method=method,
            url=url,
            headers=headers,
            content=body,
            follow_redirects=True
        )

        response = await stream_request.__aenter__()

        # Prepare response headers
        response_headers = dict(response.headers)
        response_headers.pop("content-length", None)  # Remove content-length for streaming

        async def stream_generator():
            try:
                async for chunk in response.aiter_bytes():
                    if chunk:  # Only yield non-empty chunks
                        yield chunk
            finally:
                await stream_request.__aexit__(None, None, None)

        return StreamingResponse(
            stream_generator(),
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type", "text/event-stream")
        )

    async def _handle_regular_request(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Response:
        """
        Handle a regular (non-streaming) request to LangGraph server.
        
        Args:
            method: HTTP method
            url: Target URL
            headers: Request headers
            body: Request body
            
        Returns:
            Response: Response from LangGraph
        """
        response = await self.client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
            follow_redirects=True
        )

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.headers.get("content-type")
        )

    async def cleanup(self):
        """Clean up resources when shutting down."""
        await self.client.aclose()
        logger.info("LangGraph proxy middleware cleaned up")
