import numpy as np
import matplotlib.cm as cm
from src.util import normalize
import torch
from src.evaluate import evaluate
from src.inverses import inverse
import open3d as o3d
import open3d.core as o3c
import json

def create_orthogonal_image( model, sample_count, surface_eps, gradient_step, refinement_steps ):
    device_torch = torch.device(0)
    BORDES = [1, -1]
    OFFSETPLANO = 1
    LADO = int(np.sqrt(sample_count))

    i_1, i_2 = np.meshgrid( np.linspace(BORDES[0], BORDES[1], LADO), np.linspace(BORDES[0], BORDES[1], LADO) )
    samples = np.concatenate(
                    np.concatenate( np.array([np.expand_dims(i_1, 2), 
                                            np.expand_dims(i_2, 2), 
                                            np.expand_dims(np.ones_like(i_1) * OFFSETPLANO, 2)])
                                    , axis=2 ),
                    axis=0)


    mask = np.ones(sample_count, dtype=np.bool8)
    hits = np.zeros(sample_count, dtype=np.bool8)
    while np.sum(mask) > 0:
        max_batch = 64 ** 3
        head = 0
        
        inputs = samples[ mask ]
        udfs = np.zeros( len(inputs) )

        while head < sample_count:
            inputs_subset = torch.from_numpy(inputs[head:min(head + max_batch, sample_count), :]).float().to(device_torch)
            x, y =  model(inputs_subset).values()
        
            udfs[head:min(head + max_batch, sample_count)] = torch.where( y < 0, y,torch.sqrt( y) ).squeeze().detach().cpu()
            head += max_batch

        hits[mask] += udfs < surface_eps
        mask[mask] *= udfs > surface_eps
        samples[mask] -= np.hstack( [ np.zeros( (len(udfs[udfs > surface_eps]), 2) ), np.expand_dims( udfs[udfs > surface_eps], 1 )] )


        mask *= samples[:, 2] >= -1

    values = []
    for _ in range(refinement_steps):
        max_batch = 64 ** 3
        head = 0
        
        inputs = samples[ hits ]
        gradients = np.zeros((len(inputs), 3))

        while head < sample_count:
            inputs_subset = torch.from_numpy(inputs[head:min(head + max_batch, sample_count), :]).float().to(device_torch)
            x, y =  model(inputs_subset).values()

            y.sum().backward()
            udfs = y.squeeze().detach().cpu().numpy()
            if len(udfs) > 0:
                values.append(y.squeeze().detach().cpu().numpy())
        
            gradients[head:min(head + max_batch, sample_count)] = x.grad.detach().cpu()
            head += max_batch

        samples[hits] -= gradients * gradient_step

    cmap = cm.get_cmap('turbo')
    return cmap( (np.clip( samples[:, 2].reshape((LADO, LADO)), -1, 1) + np.ones((LADO, LADO))) / 2 )[:,:,:3], values

def create_projectional_image( model, sample_count, surface_eps, gradient_eps, alpha, gt_mode, refinement_steps, directions, image, light_position, shininess=40, max_iterations=30, device=torch.device(0) ):
    # image es una lista de puntos. Tengo un rayo por cada punto en la imagen. Los rayos salen con dirección norm(image_i - origin) desde el punto mismo.
    LADO = int(np.sqrt(sample_count))

    alive = np.ones(sample_count, dtype=np.bool8)
    hits = np.zeros(sample_count, dtype=np.bool8)

    samples = image.copy()
    iteration = 0
    while np.sum(alive) > 0 and iteration < max_iterations:
        gradients = np.zeros_like(samples[alive])
        udfs = evaluate( model, samples[ alive ], gradients=gradients, device=device)

        gradient_norms = np.sum( gradients ** 2, axis=-1)

        steps = inverse( gt_mode, udfs, alpha )

        samples[alive] += directions[alive] * np.hstack([steps, steps, steps])

        threshold_mask = np.logical_and(gradient_norms < gradient_eps, steps.flatten() < surface_eps)
        indomain_mask = np.logical_and( np.all( samples[alive] > -1, axis=1 ), np.all( samples[alive] < 1, axis=1 ))
        hits[alive] += np.logical_and( threshold_mask, indomain_mask)
        alive[alive] *= np.logical_and( np.logical_not(threshold_mask), indomain_mask )
        
        iteration += 1

    
    if np.sum(hits) == 0:
        raise ValueError(f"Ray tracing did not converge in {max_iterations} iterations to any point at distance {surface_eps} or lower from surface.")

    amount_hits = np.sum(hits)
    hessians = np.zeros( (amount_hits, 3, 3) )
    gradients = np.zeros((amount_hits, 3))

    for _ in range(refinement_steps):    
        udfs = evaluate( model, samples[hits], gradients=gradients, device=device)
        steps = inverse( gt_mode, udfs, alpha, min_step=0 )
        samples[hits] -= normalize(gradients) * steps

    hessians = np.zeros((amount_hits, 3, 3))
    udfs = evaluate( model, samples[hits], gradients=gradients, hessians=hessians, device=device)

    if gt_mode == 'siren':
        normals = normalize(gradients)
    else:
        normals = np.array( [ np.linalg.eigh(hessian)[1][:,2] for hessian in hessians ] )
        # podria ser que las normales apunten para el otro lado. las tengo que invertir si  < direccion, normal > = cos(tita) > 0
        normals *= np.where( np.expand_dims(np.sum(normals * directions[hits], axis=1),1) > 0, -1 * np.ones( (normals.shape[0], 1)), np.ones( (normals.shape[0], 1)) )
    

    return phong_shading(light_position, shininess, hits, samples, normals).reshape((LADO,LADO,3))  #final_samples, np.linalg.norm( gradients, axis=1)


def phong_shading(light_position, shininess, hits, samples, normals):
    light_directions = normalize( np.tile( light_position, (normals.shape[0],1) ) - samples[hits] )
    lambertian = np.max( [np.expand_dims(np.sum(normals * light_directions, axis=1),1), np.zeros((normals.shape[0],1))], axis=0 )
    
    reflect = lambda I, N : I - (2 * np.expand_dims( np.sum(N * I, axis=1),1)) * N
    R = reflect( (-1 * light_directions), normals )
    V = normalize(samples[hits])
    spec_angles = np.max( [np.sum( R * V, axis=1 ), np.zeros(normals.shape[0])], axis=0)

    specular = np.zeros_like(lambertian)
    specular[lambertian > 0] = np.expand_dims(np.power(spec_angles, shininess),1)[lambertian > 0]

    colors = np.ones_like(samples)

    diffuse_color = np.array([0.7, 0.7, 0.7] )
    specular_color = np.array([1, 1, 1])
    ambient_color = np.array( [0.2, 0.2, 0.2])
    colors[hits] = np.clip( 
        np.tile( diffuse_color, (normals.shape[0],1)) * lambertian + 
        np.tile( specular_color, (normals.shape[0],1)) * specular +
        ambient_color , 0, 1)
    
    return colors

def create_projectional_image_gt( mesh_file, sample_count, directions, image, light_position, shininess=40, surface_eps=0.001, max_iterations=30 ):
    # image es una lista de puntos. Tengo un rayo por cada punto en la imagen. Los rayos salen con dirección norm(image_i - origin) desde el punto mismo.
    LADO = int(np.sqrt(sample_count))

    mesh = o3d.t.io.read_triangle_mesh(mesh_file)

    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(mesh)

    alive = np.ones(sample_count, dtype=np.bool8)
    hits = np.zeros(sample_count, dtype=np.bool8)
    samples = image.copy()
    iteration = 0
    while np.sum(alive) > 0 and iteration < max_iterations:
        udfs = np.expand_dims(scene.compute_distance( o3c.Tensor(samples[alive], dtype=o3c.float32) ).numpy(), -1)

        samples[alive] += directions[alive] * np.hstack([udfs, udfs, udfs])

        mask = udfs.squeeze(-1) < surface_eps
        hits[alive] += mask
        alive[alive] *= np.logical_not(mask)

        alive *= np.logical_and( np.all( samples > -1.3, axis=1 ), np.all( samples < 1.3, axis=1 ) )
        
        iteration += 1
    
    if np.sum(hits) == 0:
        raise ValueError(f"Ray tracing did not converge in {max_iterations} iterations to any point at distance {surface_eps} or lower from surface.")

    grad_eps = 0.0001
    normals = normalize( np.vstack( [
        (scene.compute_signed_distance( o3c.Tensor(samples[hits] + np.tile( np.eye(1, 3, i), (np.sum(hits),1)) * grad_eps, dtype=o3c.float32) ).numpy() -
        scene.compute_signed_distance( o3c.Tensor(samples[hits] - np.tile( np.eye(1, 3, i), (np.sum(hits),1)) * grad_eps, dtype=o3c.float32) ).numpy()) / (2*grad_eps)
        for i in range(3)]).T )
    
    normals *= np.where( np.expand_dims(np.sum(normals * directions[hits], axis=1),1) > 0, -1 * np.ones( (normals.shape[0], 1)), np.ones( (normals.shape[0], 1)) )

    return phong_shading(light_position, shininess, hits, samples, normals).reshape((LADO,LADO,3))  #final_samples, np.linalg.norm( gradients, axis=1)
