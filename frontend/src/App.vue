<template>
  <div>
    <div class="nav">
      <router-link to="/">今日数据</router-link>
      <router-link to="/history">历史记录</router-link>
      <router-link to="/funds">基金方向库</router-link>
    </div>
    <div class="container">
      <router-view />
    </div>
  </div>
</template>
