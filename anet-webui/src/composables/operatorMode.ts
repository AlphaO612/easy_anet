import { ref, watch } from 'vue'

export const advanced = ref(localStorage.getItem('anet:operator-mode') === 'advanced')
watch(advanced, value => localStorage.setItem('anet:operator-mode', value ? 'advanced' : 'simple'))
