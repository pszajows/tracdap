/*
 * Copyright 2022 Accenture Global Solutions Limited
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

package org.finos.tracdap.svc.orch.service;

import org.finos.tracdap.api.MetadataBatchRequest;
import org.finos.tracdap.api.MetadataBatchResponse;
import org.finos.tracdap.api.MetadataWriteRequest;
import org.finos.tracdap.api.TrustedMetadataApiGrpc;
import org.finos.tracdap.common.auth.GrpcClientAuth;
import org.finos.tracdap.common.exception.EUnexpected;
import org.finos.tracdap.common.grpc.GrpcClientWrap;
import org.finos.tracdap.common.metadata.MetadataCodec;
import org.finos.tracdap.common.metadata.MetadataConstants;
import org.finos.tracdap.common.metadata.MetadataUtil;
import org.finos.tracdap.config.JobConfig;
import org.finos.tracdap.config.PlatformConfig;
import org.finos.tracdap.config.RuntimeConfig;
import org.finos.tracdap.metadata.*;
import org.finos.tracdap.svc.orch.cache.JobState;
import org.finos.tracdap.svc.orch.jobs.JobLogic;

import io.grpc.MethodDescriptor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.stream.Collectors;

import static org.finos.tracdap.common.metadata.MetadataCodec.encodeValue;
import static org.finos.tracdap.common.metadata.MetadataConstants.*;
import static org.finos.tracdap.common.metadata.MetadataUtil.selectorFor;


public class JobLifecycle {

    private static final MethodDescriptor<MetadataWriteRequest, TagHeader> CREATE_OBJECT_METHOD = TrustedMetadataApiGrpc.getCreateObjectMethod();
    private static final MethodDescriptor<MetadataWriteRequest, TagHeader> UPDATE_OBJECT_METHOD = TrustedMetadataApiGrpc.getUpdateObjectMethod();
    private static final MethodDescriptor<MetadataWriteRequest, TagHeader> UPDATE_TAG_METHOD = TrustedMetadataApiGrpc.getUpdateTagMethod();
    private static final MethodDescriptor<MetadataWriteRequest, TagHeader> PREALLOCATE_ID_METHOD = TrustedMetadataApiGrpc.getPreallocateIdMethod();
    private static final MethodDescriptor<MetadataWriteRequest, TagHeader> CREATE_PREALLOCATED_OBJECT_METHOD = TrustedMetadataApiGrpc.getCreatePreallocatedObjectMethod();
    private static final MethodDescriptor<MetadataBatchRequest, MetadataBatchResponse> READ_BATCH_METHOD = TrustedMetadataApiGrpc.getReadBatchMethod();

    private final Logger log = LoggerFactory.getLogger(JobLifecycle.class);

    private final PlatformConfig platformConfig;
    private final TrustedMetadataApiGrpc.TrustedMetadataApiBlockingStub metaClient;
    private final GrpcClientWrap grpcWrap;

    public JobLifecycle(
            PlatformConfig platformConfig,
            TrustedMetadataApiGrpc.TrustedMetadataApiBlockingStub metaClient) {

        this.platformConfig = platformConfig;
        this.metaClient = metaClient;
        this.grpcWrap = new GrpcClientWrap(getClass());
    }

    JobState assembleAndValidate(JobState jobState) {

        jobState = applyTransform(jobState);
        jobState = loadResources(jobState);
        jobState = allocateResultIds(jobState);
        jobState = buildJobConfig(jobState);

        return jobState;

        // static validate
        // semantic validate
    }

    JobState applyTransform(JobState jobState) {

        var logic = JobLogic.forJobType(jobState.jobType);

        jobState.definition = logic.applyTransform(jobState.definition, platformConfig);

        return jobState;
    }

    JobState loadResources(JobState jobState) {

        var jobLogic = JobLogic.forJobType(jobState.jobType);
        var resources = jobLogic.requiredMetadata(jobState.definition);

        if (resources.isEmpty()) {
            log.info("No additional metadata required");
            return jobState;
        }

        return loadResources(jobState, resources);
    }

    JobState loadResources(JobState jobState, List<TagSelector> resources) {

        log.info("Loading additional required metadata...");

        var orderedKeys = new ArrayList<String>(resources.size());
        var orderedSelectors = new ArrayList<TagSelector>(resources.size());

        for (var selector : resources) {
            orderedKeys.add(MetadataUtil.objectKey(selector));
            orderedSelectors.add(selector);
        }

        var batchRequest = MetadataBatchRequest.newBuilder()
                .setTenant(jobState.tenant)
                .addAllSelector(orderedSelectors)
                .build();

        var client = GrpcClientAuth.applyIfAvailable(metaClient, jobState.ownerToken);
        var batchResponse = grpcWrap.unaryCall(READ_BATCH_METHOD, batchRequest, client::readBatch);

        return loadResourcesResponse(jobState, orderedKeys, batchResponse);
    }

    JobState loadResourcesResponse(
            JobState jobState, List<String> mappingKeys,
            MetadataBatchResponse batchResponse) {

        if (batchResponse.getTagCount() != mappingKeys.size())
            throw new EUnexpected();

        var jobLogic = JobLogic.forJobType(jobState.jobType);

        var resources = new HashMap<String, ObjectDefinition>(mappingKeys.size());
        var mappings = new HashMap<String, TagHeader>(mappingKeys.size());

        for (var resourceIndex = 0; resourceIndex < mappingKeys.size(); resourceIndex++) {

            var resourceTag = batchResponse.getTag(resourceIndex);
            var resourceKey = MetadataUtil.objectKey(resourceTag.getHeader());
            var mappingKey = mappingKeys.get(resourceIndex);

            resources.put(resourceKey, resourceTag.getDefinition());
            mappings.put(mappingKey, resourceTag.getHeader());
        }

        jobState.resources.putAll(resources);
        jobState.resourceMapping.putAll(mappings);

        var extraResources = jobLogic.requiredMetadata(resources).stream()
                .filter(selector -> !jobState.resources.containsKey(MetadataUtil.objectKey(selector)))
                .filter(selector -> !jobState.resourceMapping.containsKey(MetadataUtil.objectKey(selector)))
                .collect(Collectors.toList());

        if (!extraResources.isEmpty())
            return loadResources(jobState, extraResources);

        return jobState;
    }

    JobState allocateResultIds(JobState jobState) {

        // TODO: Single job timestamp - requires changes in meta svc for this to actually be used
        // meta svc must accept object timestamps as out-of-band gRPC metadata for trusted API calls
        var jobTimestamp = Instant.now();

        var jobLogic = JobLogic.forJobType(jobState.jobType);

        var priorResultIds = jobLogic.priorResultIds(
                jobState.definition,
                jobState.resources, jobState.resourceMapping);

        var newResultIds = jobLogic.newResultIds(
                jobState.tenant, jobState.definition,
                jobState.resources, jobState.resourceMapping);

        for (var priorId : priorResultIds.entrySet()) {

            var resultId = MetadataUtil.nextObjectVersion(priorId.getValue(), jobTimestamp);
            jobState.resultMapping.put(priorId.getKey(), resultId);
        }

        for (var idRequest : newResultIds.entrySet()) {

            jobState = allocateResultId(jobState, jobTimestamp, idRequest.getKey(), idRequest.getValue());
        }

        return setResultIds(jobState);
    }

    JobState allocateResultId(
            JobState jobState, Instant jobTimestamp,
            String resultKey, MetadataWriteRequest idRequest) {

        var client = GrpcClientAuth.applyIfAvailable(metaClient, jobState.ownerToken);

        var preallocatedId = grpcWrap.unaryCall(PREALLOCATE_ID_METHOD, idRequest, client::preallocateId);
        var resultId = MetadataUtil.nextObjectVersion(preallocatedId, jobTimestamp);

        jobState.resultMapping.put(resultKey, resultId);

        return jobState;
    }

    JobState setResultIds(JobState jobState) {

        var jobLogic = JobLogic.forJobType(jobState.jobType);

        jobState.definition = jobLogic.setResultIds(
                jobState.definition, jobState.resultMapping,
                jobState.resources, jobState.resourceMapping);

        return jobState;
    }

    JobState buildJobConfig(JobState jobState) {

        jobState.jobConfig = JobConfig.newBuilder()
                //.setJobId(jobState.jobId)
                .setJob(jobState.definition)
                .putAllResources(jobState.resources)
                .putAllResourceMapping(jobState.resourceMapping)
                .putAllResultMapping(jobState.resultMapping)
                .build();

        jobState.sysConfig = RuntimeConfig.newBuilder()
                .setStorage(platformConfig.getStorage())
                .putAllRepositories(platformConfig.getRepositoriesMap())
                .build();

        return jobState;
    }

    JobState saveInitialMetadata(JobState jobState) {

        var jobObj = ObjectDefinition.newBuilder()
                .setObjectType(ObjectType.JOB)
                .setJob(jobState.definition)
                .build();

        var ctrlJobAttrs = List.of(
                TagUpdate.newBuilder()
                        .setAttrName(TRAC_JOB_TYPE_ATTR)
                        .setValue(MetadataCodec.encodeValue(jobState.jobType.toString()))
                        .build(),
                TagUpdate.newBuilder()
                        .setAttrName(TRAC_JOB_STATUS_ATTR)
                        .setValue(MetadataCodec.encodeValue(jobState.statusCode.toString()))
                        .build());

        var freeJobAttrs = jobState.jobRequest.getJobAttrsList();

        var jobWriteReq = MetadataWriteRequest.newBuilder()
                .setTenant(jobState.tenant)
                .setObjectType(ObjectType.JOB)
                .setDefinition(jobObj)
                .addAllTagUpdates(ctrlJobAttrs)
                .addAllTagUpdates(freeJobAttrs)
                .build();

        var client = GrpcClientAuth.applyIfAvailable(metaClient, jobState.ownerToken);

        var jobId = grpcWrap.unaryCall(
                CREATE_OBJECT_METHOD, jobWriteReq,
                client::createObject);

        jobState.jobId = jobId;

        jobState.jobConfig = jobState.jobConfig
                .toBuilder()
                .setJobId(jobId)
                .build();

        return jobState;
    }

    void processJobResult(JobState jobState) {

        log.info("Record job result [{}]: {}", jobState.jobKey, jobState.statusCode);

        var jobLogic = JobLogic.forJobType(jobState.jobType);

        var metaUpdates = jobState.statusCode == JobStatusCode.SUCCEEDED
                ? jobLogic.buildResultMetadata(jobState.tenant, jobState.jobConfig, jobState.jobResult)
                : List.<MetadataWriteRequest>of();

        var jobUpdate = jobState.statusCode == JobStatusCode.SUCCEEDED
                ? buildJobSucceededUpdate(jobState)
                : buildJobFailedUpdate(jobState);

        for (var update : metaUpdates) {

            var update_ = applyJobAttrs(jobState, update);
            saveResultMetadata(jobState, update_);
        }

        saveResultMetadata(jobState, jobUpdate);
    }

    private TagHeader saveResultMetadata(JobState jobState, MetadataWriteRequest update) {

        var client = GrpcClientAuth.applyIfAvailable(metaClient, jobState.ownerToken);

        if (!update.hasDefinition())
            return grpcWrap.unaryCall(UPDATE_TAG_METHOD, update, client::updateTag);

        if (!update.hasPriorVersion())
            return grpcWrap.unaryCall(CREATE_OBJECT_METHOD, update, client::createObject);

        if (update.getPriorVersion().getObjectVersion() < MetadataConstants.OBJECT_FIRST_VERSION)
            return grpcWrap.unaryCall(CREATE_PREALLOCATED_OBJECT_METHOD, update, client::createPreallocatedObject);
        else
            return grpcWrap.unaryCall(UPDATE_OBJECT_METHOD, update, client::updateObject);
    }

    private MetadataWriteRequest buildJobSucceededUpdate(JobState jobState) {

        var attrUpdates = List.of(
                TagUpdate.newBuilder()
                        .setAttrName(TRAC_JOB_STATUS_ATTR)
                        .setValue(encodeValue(jobState.statusCode.toString()))
                        .build());

        return MetadataWriteRequest.newBuilder()
                .setTenant(jobState.tenant)
                .setObjectType(ObjectType.JOB)
                .setPriorVersion(selectorFor(jobState.jobId))
                .addAllTagUpdates(attrUpdates)
                .build();
    }

    private MetadataWriteRequest buildJobFailedUpdate(JobState jobState) {

        var attrUpdates = List.of(
                TagUpdate.newBuilder()
                        .setAttrName(TRAC_JOB_STATUS_ATTR)
                        .setValue(encodeValue(jobState.statusCode.toString()))
                        .build(),
                TagUpdate.newBuilder()
                        .setAttrName(TRAC_JOB_ERROR_MESSAGE_ATTR)
                        .setValue(encodeValue(jobState.statusMessage))
                        .build());

        return MetadataWriteRequest.newBuilder()
                .setTenant(jobState.tenant)
                .setObjectType(ObjectType.JOB)
                .setPriorVersion(selectorFor(jobState.jobId))
                .addAllTagUpdates(attrUpdates)
                .build();
    }

    private MetadataWriteRequest applyJobAttrs(JobState jobState, MetadataWriteRequest request) {

        if (!request.hasDefinition())
            return request;

        var builder = request.toBuilder();

        builder.addTagUpdates(TagUpdate.newBuilder()
                .setAttrName(TRAC_UPDATE_JOB)
                .setValue(MetadataCodec.encodeValue(jobState.jobKey)));

        if (!request.hasPriorVersion() || request.getPriorVersion().getObjectVersion() == 0) {

            builder.addTagUpdates(TagUpdate.newBuilder()
                    .setAttrName(TRAC_CREATE_JOB)
                    .setValue(MetadataCodec.encodeValue(jobState.jobKey)));
        }

        return builder.build();
    }
}
