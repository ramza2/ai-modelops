import { Navigate, Route, Routes } from 'react-router-dom'
import { ClientDetailPage } from './pages/ClientDetailPage'
import { ClientsPage } from './pages/ClientsPage'
import { DashboardPage } from './pages/DashboardPage'
import { DeploymentDetailPage } from './pages/DeploymentDetailPage'
import { DeploymentObservabilityPage } from './pages/DeploymentObservabilityPage'
import { DeploymentsPage } from './pages/DeploymentsPage'
import { EndpointDetailPage } from './pages/EndpointDetailPage'
import { EndpointsPage } from './pages/EndpointsPage'
import { ModelCatalogPage } from './pages/ModelCatalogPage'
import { ModelDetailPage } from './pages/ModelDetailPage'
import { ModelsPage } from './pages/ModelsPage'
import { ModelVersionDetailPage } from './pages/ModelVersionDetailPage'
import { NodeDetailPage } from './pages/NodeDetailPage'
import { NodesPage } from './pages/NodesPage'
import { ObservabilityPage } from './pages/ObservabilityPage'
import { OperationDetailPage } from './pages/OperationDetailPage'
import { OperationsPage } from './pages/OperationsPage'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard" element={<DashboardPage />} />
      <Route path="/nodes" element={<NodesPage />} />
      <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
      <Route path="/models" element={<ModelsPage />} />
      <Route path="/models/catalog" element={<ModelCatalogPage />} />
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
      <Route path="/operations" element={<OperationsPage />} />
      <Route
        path="/operations/:operationId"
        element={<OperationDetailPage />}
      />
      <Route path="/observability" element={<ObservabilityPage />} />
      <Route
        path="/observability/deployments/:deploymentId"
        element={<DeploymentObservabilityPage />}
      />
      <Route path="/clients" element={<ClientsPage />} />
      <Route path="/clients/:clientId" element={<ClientDetailPage />} />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}
