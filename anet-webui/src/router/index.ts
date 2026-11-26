import { createRouter, createWebHistory } from 'vue-router'

import Auth from '@/ui/Auth.vue'

const Users = () => import('@/ui/Users.vue')
const UserDetail = () => import('@/ui/UserDetail.vue')
const Servers = () => import('@/ui/Servers.vue')
const Statistics = () => import('@/ui/Statistics.vue')
const Pools = () => import('@/ui/Pools.vue')
const RouteMaps = () => import('@/ui/RouteMaps.vue')
const Overview = () => import('@/ui/Overview.vue')
const Groups = () => import('@/ui/Groups.vue')
const GroupDetail = () => import('@/ui/GroupDetail.vue')
const Mailboxes = () => import('@/ui/Mailboxes.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: Auth,
      meta: { isAuth: true },
    },
    { path: '/overview', component: Overview, meta: { title: 'Топология и обзор' } },
    { path: '/users', component: Users, meta: { title: 'Пользователи и конфиги' } },
    { path: '/users/:id', component: UserDetail, meta: { title: 'Профиль пользователя' } },
    { path: '/servers', component: Servers, meta: { title: 'Серверы' } },
    { path: '/pools', component: Pools, meta: { title: 'Балансировщик и пулы' } },
    { path: '/groups', component: Groups, meta: { title: 'Группы' } },
    { path: '/groups/:id', component: GroupDetail, meta: { title: 'Параметры группы' } },
    { path: '/route-maps', component: RouteMaps, meta: { title: 'Маршруты и правила' } },
    { path: '/profile-topology', component: () => import('@/ui/ProfileTopology.vue'), meta: { title: 'Топология потоков' } },
    { path: '/statistics', component: Statistics, meta: { title: 'Статистика' } },
    { path: '/operator-incidents', component: () => import('@/ui/IncidentReports.vue'), meta: { title: 'Инциденты' } },
    { path: '/mailboxes', component: Mailboxes, meta: { title: 'Почтовые ящики' } },
  ],
})

router.beforeEach((to) => {
  const token = localStorage.getItem('token')

  if (!to.meta.isAuth && !token) {
    return '/'
  }
  if (to.meta.isAuth && token) return '/overview'
})

export default router
