/*
 * Licensed to the Fintech Open Source Foundation (FINOS) under one or
 * more contributor license agreements. See the NOTICE file distributed
 * with this work for additional information regarding copyright ownership.
 * FINOS licenses this file to you under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with the
 * License. You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.finos.tracdap.gateway.proxy.http;

import io.netty.util.ReferenceCountUtil;
import org.finos.tracdap.common.exception.EUnexpected;
import org.finos.tracdap.config.RouteConfig;

import io.netty.channel.ChannelDuplexHandler;
import io.netty.channel.ChannelHandlerContext;
import io.netty.channel.ChannelPromise;
import io.netty.handler.codec.http.*;

import javax.annotation.Nonnull;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.List;


public class Http1Proxy extends ChannelDuplexHandler {

    private final RouteConfig routeConfig;

    private final String sourcePrefix;
    private final String targetPrefix;


    public Http1Proxy(RouteConfig routeConfig) {

        this.routeConfig = routeConfig;

        // For now, route translation is a simple string replace
        // No wild-card matching, regex etc.

        this.sourcePrefix = routeConfig.getMatch().getPath();
        var rawTargetPrefix = routeConfig.getTarget().getPath();

        // We need to handle source/target paths that have different trailing slashes
        // We don't want to introduce a double slash, or missing slash, in the translated path

        if (sourcePrefix.endsWith("/") && !rawTargetPrefix.endsWith("/"))
            this.targetPrefix = rawTargetPrefix + "/";
        else if (rawTargetPrefix.endsWith("/") && !sourcePrefix.endsWith("/"))
            this.targetPrefix = rawTargetPrefix.substring(0, rawTargetPrefix.length() - 1);
        else
            this.targetPrefix = rawTargetPrefix;
    }

    @Override
    public void write(ChannelHandlerContext ctx, Object msg, ChannelPromise promise) {

        try {

            if (msg instanceof HttpRequest) {

                var sourceRequest = (HttpRequest) msg;
                var targetRequest = proxyRequest(sourceRequest);

                ctx.write(targetRequest, promise);
            }
            else if (msg instanceof HttpContent) {

                ctx.write(((HttpContent) msg).retain(), promise);
            }
            else
                throw new EUnexpected();
        }
        finally {
            ReferenceCountUtil.release(msg);
        }
    }

    @Override
    public void channelRead(@Nonnull ChannelHandlerContext ctx, @Nonnull Object msg) {

        try {

            if (msg instanceof HttpResponse) {

                var serverResponse = (HttpResponse) msg;
                var proxyResponse = proxyResponse(serverResponse);

                ctx.fireChannelRead(proxyResponse);
            }
            else if (msg instanceof HttpContent) {

                ctx.fireChannelRead(((HttpContent) msg).retain());
            }
            else
                throw new EUnexpected();
        }
        finally {
            ReferenceCountUtil.release(msg);
        }
    }

    // No-op proxy translation to get things working

    private HttpRequest proxyRequest(HttpRequest sourceRequest) {

        var sourceUri = URI.create(sourceRequest.uri());
        var sourcePath = sourceUri.getPath();

        // Match should already be checked before a request is sent to this handler
        if (!sourcePath.startsWith(this.sourcePrefix))
            throw new EUnexpected();

        var targetPath = sourcePath.replaceFirst(this.sourcePrefix, this.targetPrefix);

        var targetHeaders = new DefaultHttpHeaders();
        targetHeaders.add(sourceRequest.headers());

        if (sourceRequest.headers().contains(HttpHeaderNames.HOST)) {

            var proxyHost = translateHostHeader();
            targetHeaders.remove(HttpHeaderNames.HOST);
            targetHeaders.add(HttpHeaderNames.HOST, proxyHost);
        }

        if (sourceRequest instanceof FullHttpRequest) {

            var fullRequest = (FullHttpRequest) sourceRequest;
            var fullContent = fullRequest.content().retain();

            return new DefaultFullHttpRequest(
                    sourceRequest.protocolVersion(),
                    sourceRequest.method(),
                    targetPath,
                    fullContent,
                    targetHeaders,
                    fullRequest.trailingHeaders());
        }
        else {

            return new DefaultHttpRequest(
                    sourceRequest.protocolVersion(),
                    sourceRequest.method(),
                    targetPath,
                    targetHeaders);
        }
    }

    private HttpResponse proxyResponse(HttpResponse serverResponse) {

        var proxyHeaders = new DefaultHttpHeaders();
        proxyHeaders.add(serverResponse.headers());

        if (proxyHeaders.contains(HttpHeaderNames.LOCATION)) {

            var proxyLocation = translateLocationHeader(serverResponse.headers().get(HttpHeaderNames.LOCATION));
            proxyHeaders.remove(HttpHeaderNames.LOCATION);
            proxyHeaders.set(HttpHeaderNames.LOCATION, proxyLocation);
        }

        if (serverResponse instanceof FullHttpResponse) {

            var fullResponse = (FullHttpResponse) serverResponse;
            var fullContent = fullResponse.content().retain();

            return new DefaultFullHttpResponse(
                    fullResponse.protocolVersion(),
                    fullResponse.status(),
                    fullContent,
                    proxyHeaders,
                    fullResponse.trailingHeaders());
        }
        else {

            return new DefaultHttpResponse(
                    serverResponse.protocolVersion(),
                    serverResponse.status(),
                    proxyHeaders);
        }
    }

    private String translateHostHeader() {

        var target = routeConfig.getTarget();
        var hostAlias = target.hasHostAlias() ? target.getHostAlias() : target.getHost();

        // Do not include :<port> in host header for standard ports
        for (var scheme : List.of(HttpScheme.HTTP, HttpScheme.HTTPS)) {
            if (scheme.toString().equalsIgnoreCase(target.getScheme()) && scheme.port() == target.getPort())
                return hostAlias;
        }

        return hostAlias + ":" + target.getPort();
    }

    private String translateLocationHeader(String location) {

        var target = routeConfig.getTarget();
        var locationUri = URI.create(location);
        var locationHost = locationUri.getHost();
        var locationPort = locationUri.getPort();
        var locationPath = locationUri.getPath();

        // Use default port for scheme if no port is specified
        if (locationPort < 0) {
            for (var scheme : List.of(HttpScheme.HTTP, HttpScheme.HTTPS))
                if (scheme.toString().equalsIgnoreCase(locationUri.getScheme()))
                    locationPort = scheme.port();
        }

        // If the redirect location is in the same proxy target, rewrite the location header
        if (locationPath.startsWith(target.getPath())) {

            // Redirects specified without a host, only the leading path section needs to match
            if (locationHost == null)
                return location.replace(target.getPath(), routeConfig.getMatch().getPath());

            // If host and port are specified, these need to match as well
            if (locationHost.equals(target.getHost()) && locationPort == target.getPort()) {

                try {

                    // Using null scheme and host creates a URL starting with the path component
                    // This is interpreted to mean the current host / port / scheme
                    // Query and fragment sections need to be copied over if they are present

                    var proxyPath = locationPath.replace(target.getPath(), routeConfig.getMatch().getPath());
                    var proxyUri = new URI(
                            /* scheme = */ null, /* host = */ null,
                            proxyPath, locationUri.getQuery(), locationUri.getFragment());

                    return proxyUri.toString();
                }
                catch (URISyntaxException e) {
                    throw new EUnexpected(e);
                }
            }
        }

        // For redirects outside the proxy target, return the unaltered location from the source server
        // This could be e.g. a redirect to a totally different domain
        return location;
    }
}
