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

package org.finos.tracdap.common.metadata;

import org.finos.tracdap.common.exception.ETracInternal;
import org.finos.tracdap.common.exception.EUnexpected;
import org.finos.tracdap.metadata.ObjectDefinition;
import org.finos.tracdap.metadata.ObjectType;
import org.finos.tracdap.metadata.TagHeader;
import org.finos.tracdap.metadata.TagSelector;

import java.util.HashMap;
import java.util.Map;


public class MetadataBundle {

    private static final TagHeader NO_MAPPING = TagHeader.newBuilder()
            .setObjectType(ObjectType.OBJECT_TYPE_NOT_SET)
            .setObjectId("")
            .setObjectVersion(0)
            .setTagVersion(0)
            .build();

    public final Map<String, ObjectDefinition> resources;
    public final Map<String, TagHeader> resourceMapping;

    public MetadataBundle(Map<String, ObjectDefinition> resources, Map<String, TagHeader> resourceMapping) {
        this.resources = resources;
        this.resourceMapping = resourceMapping;
    }

    public Map<String, ObjectDefinition> getResources() {
        return resources;
    }

    public Map<String, TagHeader> getResourceMapping() {
        return resourceMapping;
    }

    public ObjectDefinition getResource(TagSelector selector) {

        var selectorKey = MetadataUtil.objectKey(selector);

        // Selector mappings are optional, fall back on using the selector key
        var objectId = resourceMapping.getOrDefault(selectorKey, NO_MAPPING);
        var objectKey = objectId == NO_MAPPING
                ? selectorKey
                : MetadataUtil.objectKey(objectId);

        var object = resources.get(objectKey);

        if (object.getObjectType() != selector.getObjectType())
            throw new EUnexpected();

        return object;
    }

    public MetadataBundle withUpdate(TagSelector selector, ObjectDefinition newObject) {

        var selectorKey = MetadataUtil.objectKey(selector);
        var updates = Map.of(selectorKey, newObject);

        return this.withUpdates(updates);
    }

    public MetadataBundle withUpdates(Map<String, ObjectDefinition> updates) {

        var newResources = new HashMap<>(resources);

        for (var update : updates.entrySet()) {

            // Selector mappings are optional, fall back on using the selector key
            var objectId = resourceMapping.getOrDefault(update.getKey(), NO_MAPPING);
            var objectKey = objectId == NO_MAPPING
                    ? update.getKey()
                    : MetadataUtil.objectKey(objectId);

            if (!resources.containsKey(objectKey))
                throw new ETracInternal("Attempt to update unknown object: [" + objectKey + "]");

            newResources.put(objectKey, update.getValue());
        }

        return new MetadataBundle(newResources, resourceMapping);
    }
}
