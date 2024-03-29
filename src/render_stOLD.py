import numpy as np
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from src.util import normalize
import torch
import torch.nn.functional as F
from src.inverses import inverse
import open3d as o3d
import open3d.core as o3c
import numpy as np
from src.diff_operators import gradient, hessian, divergence, jacobian

def evaluate(model, samples, max_batch=64**2, output_size=1, device=torch.device(0), get_gradients=False, get_normals=False, get_curvatures='none'):
    head = 0
    amount_samples = samples.shape[0]

    evaluations = np.zeros( (amount_samples, output_size))
    if get_gradients:
        gradients = np.zeros((amount_samples, 3))

    if get_normals or get_curvatures:
        normals = np.zeros((amount_samples,3))
        curvature_directions = np.zeros((amount_samples, 3, 2))
    
    if get_curvatures != 'none':
        curvatures = np.zeros((amount_samples, 1))

    while head < amount_samples:
        
        if torch.is_tensor(samples):
            inputs_subset = samples[head:min(head + max_batch, amount_samples), :]
        else:
            inputs_subset = torch.from_numpy(samples[head:min(head + max_batch, amount_samples), :]).float()

        inputs_subset = inputs_subset.to(device).unsqueeze(0)

        x, y =  model(inputs_subset).values()

        if get_gradients:
            gradient_torch = gradient(y,x)
            gradients[head:min(head + max_batch, amount_samples)] = gradient_torch.squeeze(0).detach().cpu().numpy()[..., :]

        if get_normals:
            hessians_torch = hessian(y,x)
            eigenvalues, eigenvectors = torch.linalg.eigh( hessians_torch )
            pred_normals = eigenvectors[..., 2]
            #pred_normals = F.normalize( gradient(y,x), dim=-1)
            normals[head:min(head + max_batch, amount_samples)] = pred_normals[0].detach().cpu().numpy()[..., :]
            curvature_directions[head:min(head + max_batch, amount_samples)] = eigenvectors[0,:, :,:2].detach().cpu().numpy()
            
            if get_curvatures == 'gaussian':
               shape_op, status = jacobian(pred_normals, x)

               extended_hessians = torch.zeros((shape_op.shape[1], 4,4)).to(device)
               extended_hessians[:, :3,:3] = shape_op[0, :,:]
               extended_hessians[:, :3, 3] = pred_normals
               extended_hessians[:, 3, :3] = pred_normals
               curvatures[head:min(head + max_batch, amount_samples)] = (-1 * torch.linalg.det(extended_hessians)).detach().cpu().numpy()[...,None]
            elif get_curvatures == 'mean':
               curvatures[head:min(head + max_batch, amount_samples)] = divergence( pred_normals, x ).detach().cpu().numpy()[...,:]
            
            #I = torch.tile( torch.eye(3,3), dims=(pred_normals.shape[1],1,1)).to(device)
            #NNt = torch.bmm(pred_normals[0].unsqueeze(-1), pred_normals[0].unsqueeze(-2)).to(device)
            #shape_op = torch.bmm( (I - NNt), hessians_torch[0] )
            #ks, pds = torch.linalg.eigh( shape_op )
            #if get_curvatures == 'gaussian':
            #    curvatures[head:min(head + max_batch, amount_samples)] = torch.prod(ks[..., 1:], dim=-1).detach().cpu().numpy()[...,None]
            #elif get_curvatures == 'mean':
            #    curvatures[head:min(head + max_batch, amount_samples)] = (torch.sum(ks[..., 1:], dim=-1)).detach().cpu().numpy()[...,None]
    
        evaluations[head:min(head + max_batch, amount_samples)] = y.squeeze(0).detach().cpu()
        head += max_batch

    if get_curvatures != 'none':
        return evaluations, normals, curvatures, curvature_directions
    if get_normals:
        return evaluations, normals, curvature_directions
    if get_gradients:
        return evaluations, gradients
        

def create_projectional_image( model, width, height, rays, t0, mask_rays, surface_eps, alpha, gt_mode, light_position, specular_comp, plot_curvatures, max_iterations=30, device=torch.device(0) ):
    # image es una lista de puntos. Tengo un rayo por cada punto en la imagen. Los rayos salen con dirección norm(image_i - origin) desde el punto mismo.
    hits = np.zeros_like(mask_rays, dtype=np.bool8)

    iteration = 0
    while np.sum(mask_rays) > 0 and iteration < max_iterations:
        gradients = np.zeros_like(t0[mask_rays])
        udfs, gradients = evaluate( model, t0[ mask_rays ], get_gradients=True, device=device)
        steps = inverse( gt_mode, np.abs(udfs), alpha )

        t0[mask_rays] += rays[mask_rays] * steps

        if gt_mode == 'siren':
            threshold_mask = udfs.flatten() < surface_eps
        else:
            threshold_mask = np.abs(udfs).flatten() < surface_eps
            
        indomain_mask = np.logical_and( np.all( t0[mask_rays] > -1, axis=1 ), np.all( t0[mask_rays] < 1, axis=1 ))
        hits[mask_rays] += np.logical_and( threshold_mask, indomain_mask)
        mask_rays[mask_rays] *= np.logical_and( np.logical_not(threshold_mask), indomain_mask )
        
        iteration += 1

    if np.sum(hits) == 0:
        raise ValueError(f"Ray tracing did not converge in {max_iterations} iterations to any point at distance {surface_eps} or lower from surface.")

    amount_hits = np.sum(hits)
    gradients = np.zeros((amount_hits, 3))

    if gt_mode == 'siren':
        udfs, gradients = evaluate( model, t0[hits], get_gradients=True, device=device)
        normals = normalize(gradients)
        return phong_shading(light_position, specular_comp, 40, hits, t0, normals).reshape((height,width,3)) 
    else:
        if plot_curvatures != 'none':
            udfs, normals, curvatures, curvature_directions = evaluate( model, t0[hits], get_normals=True, get_curvatures=plot_curvatures, device=device )
            # podria ser que las normales apunten para el otro lado. las tengo que invertir si  < direccion, normal > = cos(tita) > 0
            direction_alignment = np.sign(np.expand_dims(np.sum(normals * rays[hits], axis=1),1)) * -1
            normals *= direction_alignment

            if plot_curvatures == 'mean':
                curvatures *= direction_alignment / 2

            cmap = cm.get_cmap('bwr')
            curvatures = np.clip( curvatures, np.percentile(curvatures, 5), np.percentile(curvatures,95))
            curvatures -= np.min(curvatures)
            curvatures/= np.max(curvatures)

            return ward_reflectance(
                light_position, 
                hits, 
                t0, 
                normals, 
                alpha1=0.2, 
                alpha2=0.5, 
                pc1=curvature_directions[..., 0], 
                pc2=curvature_directions[..., 1] ).reshape((height,width,3))     

            #return phong_shading(light_position, specular_comp, 40, hits, t0, normals, color_map=cmap(curvatures.squeeze(1))[:,:3] ).reshape((height,width,3))     
        else:
            udfs, normals = evaluate( model, t0[hits], get_normals=True, device=device )
            direction_alignment = np.sign(np.expand_dims(np.sum(normals * rays[hits], axis=1),1)) * -1
            normals *= direction_alignment
            return phong_shading( light_position, specular_comp, 40, hits, t0, normals ).reshape((height,width,3))     


def phong_shading(light_position, specular_comp, shininess, hits, samples, normals, color_map=None):
    light_directions = normalize( np.tile( light_position, (normals.shape[0],1) ) - samples[hits] )
    lambertian = np.max( [np.expand_dims(np.sum(normals * light_directions, axis=1),1), np.zeros((normals.shape[0],1))], axis=0 )
    
    reflect = lambda I, N : I - (2 * np.expand_dims( np.sum(N * I, axis=1),1)) * N
    R = reflect( (-1 * light_directions), normals )
    V = normalize(samples[hits])
    spec_angles = np.max( [np.sum( R * V, axis=1 ), np.zeros(normals.shape[0])], axis=0)

    if specular_comp:
        specular = np.zeros_like(lambertian)
        specular[lambertian > 0] = np.expand_dims(np.power(spec_angles, shininess),1)[lambertian > 0]
    else:
        specular = 0

    colors = np.ones_like(samples)


    if color_map is None:
        diffuse_color = np.tile( np.array([0.7, 0.7, 0.7] ), (normals.shape[0],1))
        specular_color = np.tile( np.array([0.7, 0.7, 0.7] ), (normals.shape[0],1))
        ambient_color = np.tile( np.array([0.2, 0.2, 0.2] ), (normals.shape[0],1))
    else:
        diffuse_color = color_map * 0.7
        specular_color = color_map * 0.7
        ambient_color = color_map * 0.2

    colors[hits] = np.clip( 
        diffuse_color * lambertian + 
        specular_color * specular +
        ambient_color , 0, 1)
    
    return colors

def ward_reflectance(light_position, camera_position, hits, samples, normals, alpha1, alpha2, pc1, pc2, color_map=None):
    light_directions = normalize( np.tile( light_position, (normals.shape[0],1) ) - samples[hits] )
    lambertian = np.max( [np.expand_dims(np.sum(normals * light_directions, axis=1),1), np.zeros((normals.shape[0],1))], axis=0 )
    
    reflect = lambda I, N : I - (2 * np.expand_dims( np.sum(N * I, axis=1),1)) * N
    R = reflect( (-1 * light_directions), normals )
    V = normalize(samples[hits])

    colors = np.ones_like(samples)

    viewer_direcions = normalize( np.tile( camera_pos, (normals.shape[0],1) ) - samples[hits] )
    H = normalize( viewer_direcions + light_directions )
    dot = lambda x,y: np.sum( x* y, axis=-1)
    weight = 1 / (4 * np.pi * alpha1 * alpha2 * np.sqrt( dot(normals, light_directions) * dot(normals,viewer_direcions) ))
    specular = weight * np.exp(
        -2 * ( (dot(H, pc1) / alpha1)**2 + (dot(H, pc2) / alpha2)**2 ) / (1+ dot(normals, H))
    )
    specular = specular[...,None]
    specular = np.nan_to_num(specular)

    if color_map is None:
        diffuse_color = np.tile( np.array([0.7, 0.7, 0.7] ), (normals.shape[0],1))
        specular_color = np.tile( np.array([0.7, 0.7, 0.7] ), (normals.shape[0],1))
        ambient_color = np.tile( np.array([0.2, 0.2, 0.2] ), (normals.shape[0],1))
    else:
        diffuse_color = color_map * 0.7
        specular_color = color_map * 0.7
        ambient_color = color_map * 0.2

    colors[hits] = np.clip( 
        diffuse_color * lambertian + 
        specular_color * specular +
        ambient_color , 0, 1)
    
    return colors


def create_projectional_image_gt( mesh_file, width, height, rays, t0, mask_rays, light_position, specular_comp,surface_eps=0.001, max_iterations=30 ):
    # image es una lista de puntos. Tengo un rayo por cada punto en la imagen. Los rayos salen con dirección norm(image_i - origin) desde el punto mismo.
    mesh = o3d.t.io.read_triangle_mesh(mesh_file)

    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(mesh)

    hits = np.zeros_like(mask_rays, dtype=np.bool8)
    iteration = 0
    while np.sum(mask_rays) > 0 and iteration < max_iterations:
        udfs = np.expand_dims(scene.compute_distance( o3c.Tensor(t0[mask_rays], dtype=o3c.float32) ).numpy(), -1)

        t0[mask_rays] += rays[mask_rays] * np.hstack([udfs, udfs, udfs])

        mask = udfs.squeeze(-1) < surface_eps
        hits[mask_rays] += mask
        mask_rays[mask_rays] *= np.logical_not(mask)

        mask_rays *= np.logical_and( np.all( t0 > -1.3, axis=1 ), np.all( t0 < 1.3, axis=1 ) )
        
        iteration += 1
    
    if np.sum(hits) == 0:
        raise ValueError(f"Ray tracing did not converge in {max_iterations} iterations to any point at distance {surface_eps} or lower from surface.")

    grad_eps = 0.0001
    normals = normalize( np.vstack( [
        (scene.compute_signed_distance( o3c.Tensor(t0[hits] + np.tile( np.eye(1, 3, i), (np.sum(hits),1)) * grad_eps, dtype=o3c.float32) ).numpy() -
        scene.compute_signed_distance( o3c.Tensor(t0[hits] - np.tile( np.eye(1, 3, i), (np.sum(hits),1)) * grad_eps, dtype=o3c.float32) ).numpy()) / (2*grad_eps)
        for i in range(3)]).T )
    
    normals *= np.where( np.expand_dims(np.sum(normals * rays[hits], axis=1),1) > 0, -1 * np.ones( (normals.shape[0], 1)), np.ones( (normals.shape[0], 1)) )

    return phong_shading(light_position, specular_comp, 40, hits, t0, normals).reshape((width,height,3)) 
