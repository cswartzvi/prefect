<template>
  <p-layout-default class="flow-run">
    <template #header>
      <PageHeadingFlowRun v-if="flowRun" :flow-run-id="flowRun.id" @delete="goToFlowRuns" />
    </template>

    <FlowRunGraphs v-if="flowRun && !isPending" :flow-run="flowRun" />

    <p-tabs v-model:selected="tab" :tabs="tabs">
      <template #details>
        <FlowRunDetails v-if="flowRun" :flow-run="flowRun" />
      </template>

      <template #logs>
        <FlowRunLogs v-if="flowRun" :flow-run="flowRun" />
      </template>

      <template #results>
        <FlowRunResults v-if="flowRun" :flow-run="flowRun" />
      </template>

      <template #artifacts>
        <FlowRunArtifacts v-if="flowRun" :flow-run="flowRun" />
      </template>

      <template #task-runs>
        <FlowRunTaskRuns v-if="flowRun" :flow-run-id="flowRun.id" />
      </template>

      <template #subflow-runs>
        <FlowRunSubFlows v-if="flowRun" :flow-run-id="flowRun.id" />
      </template>

      <template #parameters>
        <CopyableWrapper v-if="flowRun" :text-to-copy="parameters">
          <p-code-highlight lang="json" :text="parameters" class="flow-run__parameters" />
        </CopyableWrapper>
      </template>
    </p-tabs>
  </p-layout-default>
</template>

<script lang="ts" setup>
  import {
    PageHeadingFlowRun,
    FlowRunArtifacts,
    FlowRunDetails,
    FlowRunLogs,
    FlowRunTaskRuns,
    FlowRunResults,
    FlowRunSubFlows,
    useFavicon,
    useWorkspaceApi,
    useDeployment,
    getSchemaValuesWithDefaultsJson,
    CopyableWrapper,
    isPendingStateType,
    useTabs,
    httpStatus,
    useFlowRun
  } from '@prefecthq/prefect-ui-library'
  import { useRouteParam, useRouteQueryParam } from '@prefecthq/vue-compositions'
  import { computed, watchEffect } from 'vue'
  import { useRouter } from 'vue-router'
  import FlowRunGraphs from '@/components/FlowRunGraphs.vue'
  import { usePageTitle } from '@/compositions/usePageTitle'
  import { routes } from '@/router'

  const router = useRouter()
  const flowRunId = useRouteParam('flowRunId')

  const api = useWorkspaceApi()
  const { flowRun, subscription: flowRunSubscription } = useFlowRun(flowRunId, { interval: 5000 })
  const deploymentId = computed(() => flowRun.value?.deploymentId)
  const { deployment } = useDeployment(deploymentId)

  const isPending = computed(() => {
    return flowRun.value?.stateType ? isPendingStateType(flowRun.value.stateType) : true
  })
  const computedTabs = computed(() => [
    { label: 'Logs' },
    { label: 'Task Runs', hidden: isPending.value },
    { label: 'Subflow Runs', hidden: isPending.value },
    { label: 'Results', hidden: isPending.value },
    { label: 'Artifacts', hidden: isPending.value },
    { label: 'Details' },
    { label: 'Parameters' },
  ])
  const tab = useRouteQueryParam('tab', 'Logs')
  const { tabs } = useTabs(computedTabs, tab)

  const flowRunParameters = computed(() => flowRun.value?.parameters ?? {})
  const deploymentSchema = computed(() => deployment.value?.parameterOpenApiSchema ?? {})
  const parameters = computed(() => getSchemaValuesWithDefaultsJson(flowRunParameters.value, deploymentSchema.value))

  function goToFlowRuns(): void {
    router.push(routes.flowRuns())
  }

  const stateType = computed(() => flowRun.value?.stateType)
  useFavicon(stateType)

  const title = computed(() => {
    if (!flowRun.value) {
      return 'Flow Run'
    }
    return `Flow Run: ${flowRun.value.name}`
  })
  usePageTitle(title)

  watchEffect(() => {
    if (flowRunSubscription.error) {
      const status = httpStatus(flowRunSubscription.error)

      if (status.isInRange('clientError')) {
        router.replace(routes[404]())
      }
    }
  })
</script>

<style>
.flow-run { @apply
  items-start
}

.flow-run__logs { @apply
  max-h-screen
}

.flow-run__header-meta { @apply
  flex
  gap-2
  items-center
  xl:hidden
}

.flow-run__parameters { @apply
  px-4
  py-3
}
</style>
