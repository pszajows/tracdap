/*
 * Copyright 2024 Accenture Global Solutions Limited
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.finos.tracdap.common.config;

import java.util.Map;

public class ServiceProperties {

    public static final String GATEWAY_HTTP_PREFIX = "gateway.http.prefix";
    public static final String NETWORK_IDLE_TIMEOUT = "network.idleTimeout";

    public static final int NETWORK_IDLE_TIMEOUT_DEFAULT = 30;  // seconds
}
