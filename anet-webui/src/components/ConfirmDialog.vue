<script setup lang="ts">
import { confirmState, useConfirmDialog } from '@/composables/useConfirmDialog'

const { finish } = useConfirmDialog()
</script>

<template>
  <v-dialog
    :model-value="confirmState.open"
    max-width="460"
    @update:model-value="(val) => { if (!val) finish(false) }"
  >
    <v-card>
      <v-card-title class="text-h6 pt-5 px-6">
        {{ confirmState.title }}
      </v-card-title>
      <v-card-text class="px-6 text-body-2 text-medium-emphasis">
        {{ confirmState.message }}
      </v-card-text>
      <v-card-actions class="px-6 pb-4">
        <v-spacer />
        <v-btn variant="text" @click="finish(false)">
          {{ confirmState.cancelText }}
        </v-btn>
        <v-btn :color="confirmState.confirmColor" variant="flat" @click="finish(true)">
          {{ confirmState.confirmText }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
