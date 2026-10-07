import { Navigate, Route, Routes } from 'react-router-dom'
import { DashboardPage } from './pages/DashboardPage'
import { DeploymentDetailPage } from './pages/DeploymentDetailPage'
import { DeploymentsPage } from './pages/DeploymentsPage'
import { EndpointDetailPage } from './pages/EndpointDetailPage'
import { EndpointsPage } from './pages/EndpointsPage'
import { ModelDetailPage } from './pages/ModelDetailPage'
import { ModelsPage } from './pages/ModelsPage'
import { ModelVersionDetailPage } from './pages/ModelVersionDetailPage'
import { NodeDetailPage } from './pages/NodeDetailPage'
import { NodesPage } from './pages/NodesPage'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard" element={<DashboardPage />} />
      <Route path="/nodes" element={<NodesPage />} />
      <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
      <Route path="/models" element={<ModelsPage />} />
      <Route path="/models/:modelId" element={<ModelDetailPage />} />
      <Route
        path="/model-versions/:versionId"
        element={<ModelVersionDetailPage />}
      />
      <Route path="/deployments" element={<DeploymentsPage />} />
      <Route
        path="/deployments/:deploymentId"
        element={<DeploymentDetailPage />}
      />
      <Route path="/endpoints" element={<EndpointsPage />} />
      <Route
        path="/endpoints/:endpointId"
        element={<EndpointDetailPage />}
      />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}
