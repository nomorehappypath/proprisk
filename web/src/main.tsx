import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { installErrorReporter } from './lib/errorReporter'
import App from './App'

installErrorReporter()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
