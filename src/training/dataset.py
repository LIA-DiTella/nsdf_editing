import math
import numpy as np
import open3d as o3d
import open3d.core as o3c
import torch
from torch.distributions.multivariate_normal import MultivariateNormal
from torch.distributions.normal import Normal
from torch.utils.data import IterableDataset
import json

def readJson( path: str ):
    """Reads a JSON file with position, normal and curvature info as generated by **src/preprocess.py**

    Parameters
    ----------
    path: str, PathLike
        Path to the JSON file.

    Returns
    -------
    list_mesh: list[ o3d.t.geometry.TriangleMesh ]
        List of the fully constructed Open3D Triangle Meshes. By default, the meshes are
        allocated on the CPU:0 device.

    See Also
    --------
    o3d.t.geometry.TriangleMesh
    """
    # Reading the PLY file with curvature info
    meshes = []
    means = []
    covs = []
    amountBranches = 0

    with open(path, "r") as jsonFile:
        data = json.load( jsonFile )
        amountBranches = data['amount_branches']
        for branch in data['branches']:
            for joint in branch['joints']:

                means.append( torch.from_numpy( np.array(joint['mean']) ))
                covs.append( torch.from_numpy( np.array(joint['cov']) ))     

                device = o3c.Device("CPU:0")
                mesh = o3d.t.geometry.TriangleMesh(device)
                mesh.vertex["positions"] = o3c.Tensor(np.asarray(joint['vertices']), dtype=o3c.float32)
                mesh.vertex["normals"] = o3c.Tensor(np.asarray(joint['normals']), dtype=o3c.float32)
                mesh.vertex["curvature"] = o3c.Tensor(np.asarray(joint['curvature']), dtype=o3c.float32)
                mesh.triangle["indices"] = o3c.Tensor(np.asarray(joint['triangles']), dtype=o3c.int32)
                meshes.append( mesh )
        
    return amountBranches, meshes, means, covs

def getCurvatureBins(curvatures: torch.Tensor, percentiles: list) -> list:
    """Bins the curvature values according to `percentiles`.

    Parameters
    ----------
    curvatures: list [ torch.Tensor ]
        List of Tensors with the curvature values for the vertices of each mesh.

    percentiles: list
        List with the percentiles. torch.quantile accepts only
        values in range [0, 1].

    Returns
    -------
    quantiles: list
        A list of lists with len(percentiles) + 2 elements composed by the minimum, the
        len(percentiles) values and the maximum values for curvature per mesh.

    See Also
        try:
    --------
    torch.quantile
    """
    allBins = []
    for curvature in curvatures:
        q = torch.quantile(curvature, torch.Tensor(percentiles))
        bins = [curvature.min().item(), curvature.max().item()]
        # Hack to insert elements of a list inside another list.
        bins[1:1] = q.data.tolist()
        allBins.append(bins)

    return allBins

def sampleTrainingData(
        meshes: list,
        samplesOnSurface: int,
        samplesOffSurface: int,
        scenes: list,
        distributions: list,
        onSurfaceExceptions: list = [],
        domainBounds: tuple = ([-1, -1, -1], [1, 1, 1]),
        curvatureFractions: list = [],
        curvatureBins: list = [],
):
    """Creates a set of training data with coordinates, normals and SDF
    values.

    Parameters
    ----------
    mesh: list[o3d.t.geometry.TriangleMesh]
        list of tensor-backed Open3D meshes.

    samplesOnSurface: int
        # of points to sample from each mesh.

    samplesOffSurface: int
        # of points to sample from the domain per mesh. Note that we sample points
        uniformely at random from the domain.

    onSurfaceExceptions: list, optional
        List of list of points that cannot be used for training, i.e. test set of points.

    domainBounds: tuple[np.array, np.array]
        Bounds to use when sampling points from the domain.

    scenes: o3d.t.geometry.RaycastingScene
        Open3D raycasting scenes to use when querying SDF for domain points.

    curvatureFractions: list, optional
        The fractions of points to sample per curvature band.

    curvatureBins: list
        The curvature values to use when defining low, medium and high
        curvatures.

    Returns
    -------
    fullSamples: torch.Tensor
    fullDistances: torch.Tensor
    fullNormals: torch.Tensor
    fullSDFs: torch.Tensor

    See Also
    --------
    _sample_on_surface, _lowMedHighCurvSegmentation
    """
    surfacePoints = torch.cat([
        pointSegmentationByCurvature(
        mesh,
        samplesOnSurface,
        bins,
        curvatureFractions,
        exceptions
    ) for mesh, exceptions, bins in zip(meshes, onSurfaceExceptions, curvatureBins)], dim=0)

    surfacePoints = torch.from_numpy(surfacePoints.numpy())

    domainPoints = [ o3c.Tensor(np.random.uniform(
        domainBounds[0], domainBounds[1],
        (samplesOffSurface, 3)
    ), dtype=o3c.Dtype.Float32) for _ in range(len(meshes))]

    domainSDFs = torch.cat( [ torch.from_numpy(scene.compute_distance(points).numpy()) for scene, points in zip(scenes, domainPoints)], dim=0)
    domainSDFs = torch.from_numpy(domainSDFs.numpy())
    domainPoints = torch.cat( [ torch.from_numpy(points.numpy()) for points in domainPoints ] )

    domainNormals = torch.zeros_like(domainPoints)

    fullSamples = torch.row_stack((
        surfacePoints[..., :3],
        domainPoints
    ))
    fullNormals = torch.row_stack((
        surfacePoints[..., 3:6],
        domainNormals
    ))
    fullSDFs = torch.cat((
        torch.zeros(len(surfacePoints)),
        domainSDFs
    )).unsqueeze(1)
    fullCurvatures = torch.cat((
        surfacePoints[..., -1],
        torch.zeros(len(domainPoints))
    )).unsqueeze(1)

    
    fullOnSurfDistances = torch.cat( [
        torch.abs( dist.sample( torch.Size([ samplesOnSurface ]) )) for dist in distributions
    ] )

    fullOffSurfDistances = torch.cat( [
        torch.abs( dist.sample( torch.Size([ samplesOffSurface ]) )) for dist in distributions
    ] )

    fullDistances = torch.cat( (fullOnSurfDistances, fullOffSurfDistances))

    return torch.column_stack( [fullDistances, fullSamples]).float(), fullNormals.float(), fullSDFs.float(), fullCurvatures.float()

def pointSegmentationByCurvature(
        mesh: o3d.t.geometry.TriangleMesh,
        amountOfSamples: int,
        binEdges: np.array,
        proportions: np.array,
        exceptions: list = []
):
    """Samples `n_points` points from `mesh` based on their curvature.

    This function is based on `i3d.dataset.lowMedHighCurvSegmentation`.

    Parameters
    ----------
    mesh: o3d.t.geometry.TriangleMesh,
        The mesh to sample points from.

    amountOfSamples: int
        Number of samples to fetch.

    binEdges: np.array
        The [minimum, low-medium threshold, medium-high threshold, maximum]
        curvature values in `mesh`. These values define thresholds between low
        and medium curvature values, and medium to high curvatures.

    proportions: np.array
        The percentage of points to fetch for each curvature band per batch of
        `n_samples`.

    Returns
    -------
    samples: torch.Tensor
        The vertices sampled from `mesh`.
    """

    def fillBin( points, curvatures, amountSamples, lowerBound, upperBound ):
        pointsInBounds = points[(curvatures >= lowerBound) & (curvatures <= upperBound), ...]
        maskSampledPoints = np.random.choice(
            range(pointsInBounds.shape[0]),
            size=amountSamples,
            replace=True if amountSamples > pointsInBounds.shape[0] else False
        )
        return pointsInBounds[maskSampledPoints, ...]

    pointsOnSurface = torch.column_stack((
        torch.from_numpy(mesh.vertex["positions"].numpy()),
        torch.from_numpy(mesh.vertex["normals"].numpy()),
        torch.from_numpy(mesh.vertex["curvature"].numpy())
    ))

    if exceptions:
        index = torch.Tensor(
            list(set(range(pointsOnSurface.shape[0])) - set(exceptions)),
        ).int()
        pointsOnSurface = torch.index_select(
            pointsOnSurface, dim=0, index=index
        )

    curvatures = pointsOnSurface[..., -1]

    pointsLowCurvature = fillBin( pointsOnSurface, curvatures, int(math.floor(proportions[0] * amountOfSamples)), binEdges[0], binEdges[1])
    pointsMedCurvature = fillBin( pointsOnSurface, curvatures, int(math.ceil(proportions[1] * amountOfSamples)), binEdges[1], binEdges[2])
    pointsHighCurvature = fillBin( pointsOnSurface, curvatures, amountOfSamples - pointsLowCurvature.shape[0] - pointsMedCurvature.shape[0] , binEdges[2], binEdges[3])

    return torch.cat((
        pointsLowCurvature,
        pointsMedCurvature,
        pointsHighCurvature
    ), dim=0)



class PointCloud(IterableDataset):
    """SDF Point Cloud dataset.

    Parameters
    ----------
    jsonPath: str
        Path to the json file obtained from preprocessing step.

    batchSize: integer, optional
        Used for fetching `batchSize` at every call of `__getitem__`.

    curvatureFractions: list, optional
        The fractions of points to sample per curvature band.

    curvaturePercentiles: list, optional
        The curvature percentiles to use when defining low, medium and high
        curvatures. 

    References
    ----------
    [1] Sitzmann, V., Martel, J. N. P., Bergman, A. W., Lindell, D. B.,
    & Wetzstein, G. (2020). Implicit Neural Representations with Periodic
    Activation Functions. ArXiv. Retrieved from http://arxiv.org/abs/2006.09661
    """
    def __init__(self, jsonPath: str,
                 batchSize: int,
                 batchesPerEpoch : int,
                 curvatureFractions: list = [],
                 curvaturePercentiles: list = []):
        super().__init__()

        print(f"Loading meshes \"{jsonPath}\".")
        self.amountBranches, self.meshes, self.means, self.covs = readJson(jsonPath)
        
        if batchSize % (2 * len(self.meshes)) != 0:
            raise ValueError(f'Batch size must be divisible by {2 * len(self.meshes)}')
        
        self.batchSize = batchSize
        print(f"Fetching {self.batchSize // 2} on-surface points per iteration.")

        self.batchesPerEpoch = batchesPerEpoch

        print("Creating point-cloud and acceleration structures.")
        self.scenes = []
        for mesh in self.meshes:
            scene = o3d.t.geometry.RaycastingScene()
            scene.add_triangles(mesh)
            self.scenes.append( scene )

        self.curvatureFractions = curvatureFractions

        self.curvatureBins = getCurvatureBins(
            [ torch.from_numpy(mesh.vertex["curvature"].numpy()) for mesh in self.meshes ],
            curvaturePercentiles
            )
        
        
    def __iter__(self):
        for _ in range(self.batchesPerEpoch):
            yield sampleTrainingData(
                meshes=self.meshes,
                samplesOnSurface=(self.batchSize // 2) // len(self.meshes),
                samplesOffSurface=(self.batchSize // 2) // len(self.meshes),
                scenes=self.scenes,
                curvatureFractions=self.curvatureFractions,
                curvatureBins=self.curvatureBins,
                distributions= [ MultivariateNormal( mean, cov ) if mean.shape[0] > 1 else Normal( mean, cov ) for mean, cov in zip(self.means, self.covs)],
                onSurfaceExceptions= [[] for _ in range(len(self.meshes))]
            )
    
if __name__ == "__main__":
    p = PointCloud(
        "results/juguete/juguete.json", batchSize=10, batchesPerEpoch=1,
        curvatureFractions=(0.2, 0.7, 0.1), curvaturePercentiles=(0.7, 0.85)
    )

    print(next(iter(p)))