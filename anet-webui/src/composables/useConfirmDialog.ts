import { reactive } from 'vue'

export interface ConfirmOptions {
  title: string
  message: string
  confirmText?: string
  cancelText?: string
  confirmColor?: 'primary' | 'error' | 'warning' | 'success'
}

interface ConfirmState extends ConfirmOptions {
  open: boolean
  resolve: ((value: boolean) => void) | null
}

export const confirmState = reactive<ConfirmState>({
  open: false,
  title: '',
  message: '',
  confirmText: 'Подтвердить',
  cancelText: 'Отмена',
  confirmColor: 'error',
  resolve: null,
})

export function useConfirmDialog() {
  const confirmAction = (options: ConfirmOptions): Promise<boolean> => {
    if (confirmState.resolve) {
      confirmState.resolve(false)
    }
    confirmState.title = options.title
    confirmState.message = options.message
    confirmState.confirmText = options.confirmText ?? 'Подтвердить'
    confirmState.cancelText = options.cancelText ?? 'Отмена'
    confirmState.confirmColor = options.confirmColor ?? 'error'
    confirmState.open = true

    return new Promise<boolean>((resolve) => {
      confirmState.resolve = resolve
    })
  }

  const finish = (accepted: boolean) => {
    confirmState.open = false
    const resolver = confirmState.resolve
    confirmState.resolve = null
    if (resolver) resolver(accepted)
  }

  return {
    confirmAction,
    finish,
  }
}
